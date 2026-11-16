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
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context, Fiber, Service
from sqlalchemy.exc import SQLAlchemyError

from common.db_errors import DbError

from .plugin_manager import (
    PluginManager,
    PluginManagerError,
    PublicPluginRecord,
    source_plugin_id,
)
from .plugin_manager_api_service import MAX_CONFIG_BODY_BYTES as MAX_CONFIG_BODY_BYTES
from .plugin_manager_api_service import PluginManagerApiService


class PluginManagerPlugin(Service[None]):
    """插件管理插件（同时是 ``ctx.plugins`` 服务的载体）。

    依赖 ``appconfig``，并从 ``[node].install_root`` 读取安装目录；管理
    HTTP 层拆给 :class:`~common.plugins.webapi.plugin_manager_api_service.PluginManagerApiService`
    （``inject = ["plugins", "webapi"]``，同包），核心不沾 webapi。
    """

    name: str = "plugin-manager"
    #: 展示名与版本：内置插件没有 plugin.json，清单元数据由插件类属性提供
    display_name: ClassVar[str] = "插件管理"
    version: ClassVar[str] = "1.0.0"
    provide: str | None = "plugins"
    inject: ClassVar[list[str]] = ["appconfig"]

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
        base_dir = config_path.parent if config_path is not None else Path.cwd()
        self._base_dir: Path = base_dir
        if not install_root.is_absolute():
            install_root = base_dir / install_root
        self.install_root: Path = install_root.resolve()
        self.preinstalled_ids: frozenset[str] = self._preinstalled_ids(
            appconfig.get("node", "plugins", "") or "",
            base_dir,
        )
        self.manager: PluginManager | None = None
        #: 管理 API 服务 fiber（PluginManagerApiService）：随本插件 fiber 自动清理
        self._api_fiber: Fiber | None = None
        #: ``db/schema-changed`` 触发的消费方复检任务（强引用防 GC）
        self._recheck_tasks: set[asyncio.Task[None]] = set()

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
            except (OSError, TypeError, ValueError, RecursionError) as exc:
                self.ctx.logger.warning("预装插件源 %s 解析 id 失败：%s", source, exc)
        return frozenset(ids)

    async def init(self) -> None:
        self.manager = PluginManager(
            self.ctx,
            self.install_root,
            self.preinstalled_ids,
            base_dir=self._base_dir,
        )
        await self.manager.start()
        # 管理 HTTP 层拆给 PluginManagerApiService（inject = ["plugins",
        # "webapi"]）：依赖就位自动挂载、换装随依赖纪元自动重挂；fiber 挂在
        # 本插件生命周期下。不能 await——webapi 缺席时 fiber 会停在 PENDING。
        self._api_fiber = self.ctx.plugin(PluginManagerApiService)
        # 「恢复已启用」不再随插件初始化执行：预装链路（ensure_installed）
        # 装配完后由 node_runtime 显式调用 restore_enabled，避免同一插件
        # 先恢复启用、再被覆盖安装重装一遍（启动期加载两次）
        # 锁步模型（DESIGN §4.5）：db 结构变化 → 复检消费方数据依赖，未过则停用并报错
        self.ctx.on("db/schema-changed", self._on_schema_changed)

    # -- db/schema-changed：消费方依赖复检 ------------------------------------
    def _on_schema_changed(self, payload: object) -> None:
        """``db/schema-changed`` 同步入口：转成异步任务复检消费方。"""
        try:
            task = asyncio.create_task(self._recheck_consumers(payload))
        except RuntimeError as exc:
            self.ctx.logger.error("数据依赖复检调度失败：%s", exc)
            return
        self._recheck_tasks.add(task)  # 强引用防 GC（RUF006）
        task.add_done_callback(self._on_recheck_done)

    def _on_recheck_done(self, task: asyncio.Task[None]) -> None:
        """观察哨：白名单之外的异常也要见光，后台任务不允许无声失败。"""
        self._recheck_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("数据依赖复检任务意外失败：%s", error)

    async def _recheck_consumers(self, payload: object) -> None:
        """复检受影响 owner 的消费方，失配即停用并记明原因（已加载的 B 不带着坏依赖继续跑）。"""
        manager = self.manager
        data = cast(dict[object, object], payload) if isinstance(payload, dict) else None
        owner = data.get("owner") if data is not None else None
        if manager is None or not isinstance(owner, str):
            return
        db = self.ctx.get("db", strict=False)
        if db is None:
            return
        try:
            failures = await db.recheck_consumers(owner)
        except (DbError, SQLAlchemyError) as exc:
            self.ctx.logger.error("消费方数据依赖复检失败（owner=%s）：%s", owner, exc)
            return
        for consumer, reason in failures:
            try:
                if consumer in manager.fibers:
                    await manager.disable(consumer, reason=reason)
                    self.ctx.logger.warning("消费方 %s 因数据依赖失效已停用：%s", consumer, reason)
            except (PluginManagerError, DbError, RuntimeError, SQLAlchemyError) as exc:
                self.ctx.logger.error("停用消费方 %s 失败：%s", consumer, exc)

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

    async def ensure_source(self, source_dir: str | Path) -> PublicPluginRecord:
        """预装源就绪一条龙（比对 → 换装如需 → 启用），单锁原子。"""
        return await self._require_manager().ensure_source(source_dir)

    async def restore_enabled(self) -> None:
        """恢复注册表里已启用的插件。

        由 ``node_runtime`` 在预装链路（ensure_installed）**之后**调用：
        预装插件此时已就绪并激活，restore 的幂等短路不会二次装载；
        预装源损坏时则回落到旧版安装（保持可用），非预装的已启用插件照常恢复。
        """
        await self._require_manager().restore_enabled()

    async def enable(self, plugin_id: str) -> PublicPluginRecord:
        """启用插件并加载进本节点运行时。"""
        return await self._require_manager().enable(plugin_id)

    async def disable(self, plugin_id: str) -> PublicPluginRecord:
        """停用插件并卸载其 fiber。"""
        return await self._require_manager().disable(plugin_id)

    async def uninstall(
        self, plugin_id: str, *, keep_data: bool = False, drop: bool = False
    ) -> None:
        """停用并删除插件包；同步清理它认领的数据表。

        ``keep_data=True`` 保留数据表（重装复用）；``drop=True`` 强制删除
        被依赖插件的数据（默认拒绝卸载）。
        """
        await self._require_manager().uninstall(
            plugin_id, keep_data=keep_data, drop=drop
        )

    async def update_config(
        self, plugin_id: str, config: Mapping[str, object]
    ) -> PublicPluginRecord:
        """部分更新插件配置（与 ``PATCH /api/plugins/{id}/config`` 同一实现）。

        运行中的插件经 ``fiber.update`` 热更新并写回 ``config.ini`` 对应节；
        未启用的只保存进安装记录，下次启用生效。内置插件拒绝（403）。
        """
        return await self._require_manager().update_config(plugin_id, config)

    async def plugin_readme(self, plugin_id: str) -> str:
        """读取插件描述文件（README.md，Markdown 文本）。"""
        return await self._require_manager().plugin_readme(plugin_id)

    async def package_bytes(self, plugin_id: str) -> bytes:
        """读取插件包原始 ZIP（保留归档，供分发/回溯）。"""
        return await asyncio.to_thread(self._require_manager().package_bytes, plugin_id)

    async def package_source(self, source_dir: str | Path) -> bytes:
        """把插件源目录打成 zip 包（插件的交付形态）。"""
        return await asyncio.to_thread(
            self._require_manager().package_source, source_dir
        )


__all__: list[str] = ["PluginManagerPlugin"]
