"""插件管理：Web UI 的安装/启停 API，并把安装能力注册为 ``ctx.plugins`` 服务。

两个入口、同一套实现：

* **HTTP**（``/api/plugins`` 等）—— 面向操作者，由主节点 Web UI 调用；
* **服务**（``ctx.plugins``）—— 面向插件。后续的主从通信插件声明
  ``inject = ["plugins"]``，收到插件包后调用 ``install_archive`` / ``enable``
  即可完成本地安装，不需要自建 :class:`PluginManager`（多份注册表会互相覆盖）。

预装插件集合（``protected`` 标记）从 ``config.ini`` 的 ``[node] plugins``
推导：列出的插件源（目录或 zip）解析出的 id 即预装——与启动时
``ensure_installed`` 自动重装的那批同源，标记跟随配置变化。

本期只做安装层；节点注册、心跳、包分发与数据回传由后续插件提供。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Service
from starlette.requests import Request
from starlette.responses import JSONResponse

from .plugin_manager import (
    MAX_ARCHIVE_SIZE,
    PluginManager,
    PluginManagerError,
    PublicPluginRecord,
    source_plugin_id,
)


class PluginManagerPlugin(Service[None]):
    """插件管理插件（同时是 ``ctx.plugins`` 服务的载体）。

    依赖 ``appconfig`` 与 ``webapi``，并从 ``[node].install_root`` 读取安装目录。
    """

    name: str = "plugin-manager"
    provide: str | None = "plugins"
    inject: ClassVar[list[str]] = ["appconfig", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        if _config is not None:
            raise TypeError("PluginManagerPlugin 配置请写入 config.ini 的 [node] 节")
        super().__init__(ctx)      # ← 最先调用：注册 ctx.plugins
        self.ctx: Context = ctx
        appconfig = ctx.appconfig
        install_root = Path(
            appconfig.get("node", "install_root", "plugins") or "plugins"
        ).expanduser()
        config_path = appconfig.path_loaded
        if not install_root.is_absolute():
            base_dir = config_path.parent if config_path is not None else Path.cwd()
            install_root = base_dir / install_root
        self.install_root: Path = install_root.resolve()
        self.preinstalled_ids: frozenset[str] = self._preinstalled_ids(
            appconfig.get("node", "plugins", "") or "",
            config_path.parent if config_path is not None else Path.cwd(),
        )
        self.manager: PluginManager | None = None

    def _preinstalled_ids(self, raw: str, base_dir: Path) -> frozenset[str]:
        """从 ``[node] plugins``（逗号分隔的插件源路径）解析预装插件 id。

        预装集合即启动时 ``ensure_installed`` 自动重装的那批插件源——
        与之同源，标记天然跟随 config.ini，不需要两处维护。单个源读不出
        id 只告警跳过（启动时 ``ensure_installed`` 会再报一次硬错误）。
        """
        ids: set[str] = set()
        for item in (part.strip() for part in raw.split(",")):
            if not item:
                continue
            source = Path(item)
            if not source.is_absolute():
                source = base_dir / source
            try:
                ids.add(source_plugin_id(source))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.ctx.logger.warning("预装插件源 %s 解析 id 失败：%s", source, exc)
        return frozenset(ids)

    async def init(self) -> None:
        loop = asyncio.get_running_loop()
        self.manager = PluginManager(
            self.ctx, loop, self.install_root, self.preinstalled_ids
        )
        self._register_routes()
        await self.manager.restore_enabled()

    # -- ctx.plugins 服务 API -------------------------------------------------
    #
    # 面向**插件**的安装能力入口（与 HTTP 层共用同一个 PluginManager 实例）。
    def _require_manager(self) -> PluginManager:
        if self.manager is None:
            raise RuntimeError("插件管理器尚未初始化")
        return self.manager

    async def list_plugins(self) -> list[PublicPluginRecord]:
        """列出已安装插件（含 kind / requires / enabled）。"""
        return await self._require_manager().list_plugins()

    async def install_archive(self, archive: bytes, *, replace: bool = False) -> PublicPluginRecord:
        """安装插件包（与 ``POST /api/plugins/upload`` 同一实现）。

        ``replace=True`` 是调用方的**显式替换确认**：同 id 已安装时覆盖换装，
        否则报 409 并携带新旧版本详情（不区分升级/降级，替换一律需确认）。
        """
        return await self._require_manager().install_archive(archive, replace=replace)

    async def install_source(self, source_dir: str | Path, *, replace: bool = False) -> PublicPluginRecord:
        """从本地插件源目录安装（如 ``master/webui``）。"""
        return await self._require_manager().install_source(source_dir, replace=replace)

    async def enable(self, plugin_id: str) -> PublicPluginRecord:
        """启用插件并加载进本节点运行时。"""
        return await self._require_manager().enable(plugin_id)

    async def disable(self, plugin_id: str) -> PublicPluginRecord:
        """停用插件并卸载其 fiber。"""
        return await self._require_manager().disable(plugin_id)

    async def uninstall(self, plugin_id: str) -> None:
        """停用并删除插件包。"""
        await self._require_manager().uninstall(plugin_id)

    async def plugin_readme(self, plugin_id: str) -> str:
        """读取插件描述文件（README.md，Markdown 文本）。"""
        return await self._require_manager().plugin_readme(plugin_id)

    async def package_bytes(self, plugin_id: str) -> bytes:
        """读取插件包原始 ZIP（保留归档，供分发/回溯）。"""
        return self._require_manager().package_bytes(plugin_id)

    async def package_source(self, source_dir: str | Path) -> bytes:
        """把插件源目录打成 zip 包（插件的交付形态）。"""
        return self._require_manager().package_source(source_dir)

    # -- HTTP 层（面向操作者）-------------------------------------------------
    def _register_routes(self) -> None:
        ctx = self.ctx

        def manager() -> PluginManager:
            if self.manager is None:
                raise RuntimeError("插件管理器尚未初始化")
            return self.manager

        def error_response(error: PluginManagerError) -> JSONResponse:
            payload: dict[str, object] = {"error": str(error)}
            if error.details:
                payload["details"] = error.details
            return ctx.webapi.json(payload, status_code=error.status_code)

        @ctx.webapi.get("/api/plugins", kind="api", name="plugins.list")
        def list_plugins(_: Request) -> dict[str, object] | JSONResponse:
            try:
                items = manager().run_from_web_thread(manager().list_plugins())
                return {"plugins": items}
            except PluginManagerError as error:
                return error_response(error)

        @ctx.webapi.post("/api/plugins/upload", kind="api", name="plugins.upload")
        async def upload_plugin(_request: Request) -> dict[str, object] | JSONResponse:
            try:
                declared_size = _request.headers.get("content-length")
                if declared_size and int(declared_size) > MAX_ARCHIVE_SIZE:
                    raise PluginManagerError("插件 ZIP 最大允许 32 MiB", 413)
                chunks: list[bytes] = []
                total_size = 0
                async for chunk in _request.stream():
                    total_size += len(chunk)
                    if total_size > MAX_ARCHIVE_SIZE:
                        raise PluginManagerError("插件 ZIP 最大允许 32 MiB", 413)
                    chunks.append(chunk)
                # replace=1 是操作者在确认弹窗后给出的显式替换确认
                replace = _request.query_params.get("replace", "") in {"1", "true", "yes"}
                item = await asyncio.to_thread(
                    manager().run_from_web_thread,
                    manager().install_archive(b"".join(chunks), replace=replace),
                )
                return {"plugin": item}
            except PluginManagerError as error:
                return error_response(error)
            except ValueError:
                return ctx.webapi.json({"error": "Content-Length 无效"}, status_code=400)

        @ctx.webapi.post("/api/plugins/{plugin_id}/enable", kind="api", name="plugins.enable")
        def enable_plugin(_: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
            try:
                item = manager().run_from_web_thread(manager().enable(plugin_id))
                return {"plugin": item}
            except PluginManagerError as error:
                return error_response(error)

        @ctx.webapi.post("/api/plugins/{plugin_id}/disable", kind="api", name="plugins.disable")
        def disable_plugin(_: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
            try:
                item = manager().run_from_web_thread(manager().disable(plugin_id))
                return {"plugin": item}
            except PluginManagerError as error:
                return error_response(error)

        @ctx.webapi.delete("/api/plugins/{plugin_id}", kind="api", name="plugins.uninstall")
        def uninstall_plugin(_: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
            try:
                manager().run_from_web_thread(manager().uninstall(plugin_id))
                return {"ok": True, "id": plugin_id}
            except PluginManagerError as error:
                return error_response(error)

        @ctx.webapi.get("/api/plugins/{plugin_id}/readme", kind="api", name="plugins.readme")
        def plugin_readme(_: Request, plugin_id: str) -> dict[str, object] | JSONResponse:
            """插件描述文件（Markdown 文本），供管理页弹窗展示。"""
            try:
                content = manager().run_from_web_thread(
                    manager().plugin_readme(plugin_id)
                )
                return {"id": plugin_id, "content": content}
            except PluginManagerError as error:
                return error_response(error)


__all__: list[str] = ["PluginManagerPlugin"]