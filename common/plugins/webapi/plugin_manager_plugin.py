"""插件管理：Web UI 的安装/启停 API，并把安装能力注册为 ``ctx.plugins`` 服务。

两个入口、同一套实现：

* **HTTP**（``/api/plugins`` 等）—— 面向操作者，由主节点 Web UI 调用；
* **服务**（``ctx.plugins``）—— 面向插件。后续的主从通信插件声明
  ``inject = ["plugins"]``，收到插件包后调用 ``install_archive`` / ``enable``
  即可完成本地安装，不需要自建 :class:`PluginManager`（多份注册表会互相覆盖）。

本期只做安装层；节点注册、心跳、包分发与数据回传由后续插件提供。
"""

from __future__ import annotations

import asyncio
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
)


class PluginManagerPlugin(Service[dict[str, str | Path]]):
    """插件管理插件（同时是 ``ctx.plugins`` 服务的载体）。"""

    name: str = "plugin-manager"
    provide: str | None = "plugins"
    inject: ClassVar[list[str]] = ["webapi"]

    def __init__(self, ctx: Context, config: dict[str, str | Path]) -> None:
        super().__init__(ctx)      # ← 最先调用：注册 ctx.plugins
        self.ctx: Context = ctx
        self.install_root: Path = Path(config["install_root"]).resolve()
        self.manager: PluginManager | None = None

    async def init(self) -> None:
        loop = asyncio.get_running_loop()
        self.manager = PluginManager(self.ctx, loop, self.install_root)
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
        """列出已安装插件（含 role / requires / enabled 状态）。"""
        return await self._require_manager().list_plugins()

    async def install_archive(self, archive: bytes) -> PublicPluginRecord:
        """安装插件包（与 ``POST /api/plugins/upload`` 同一实现）。"""
        return await self._require_manager().install_archive(archive)

    async def install_source(self, source_dir: str | Path) -> PublicPluginRecord:
        """从本地插件源目录安装（如 ``masterplugins/webui``）。"""
        return await self._require_manager().install_source(source_dir)

    async def enable(self, plugin_id: str) -> PublicPluginRecord:
        """启用插件并加载进本节点运行时。"""
        return await self._require_manager().enable(plugin_id)

    async def disable(self, plugin_id: str) -> PublicPluginRecord:
        """停用插件并卸载其 fiber。"""
        return await self._require_manager().disable(plugin_id)

    async def uninstall(self, plugin_id: str) -> None:
        """停用并删除插件包。"""
        await self._require_manager().uninstall(plugin_id)

    async def package_bytes(self, plugin_id: str) -> bytes:
        """读取插件包原始 ZIP（保留归档，供分发/回溯）。"""
        return self._require_manager().package_bytes(plugin_id)

    # -- HTTP 层（面向操作者）-------------------------------------------------
    def _register_routes(self) -> None:
        ctx = self.ctx

        def manager() -> PluginManager:
            if self.manager is None:
                raise RuntimeError("插件管理器尚未初始化")
            return self.manager

        def error_response(error: PluginManagerError) -> JSONResponse:
            return ctx.webapi.json({"error": str(error)}, status_code=error.status_code)

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
                item = await asyncio.to_thread(
                    manager().run_from_web_thread,
                    manager().install_archive(b"".join(chunks)),
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


__all__: list[str] = ["PluginManagerPlugin"]