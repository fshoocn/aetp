"""数据库插件入口：装配引擎、建库内元表并执行启动时孤儿回收。

加载后其他插件声明 ``inject = ["db"]`` 即可建表与增删改查::

    class MyPlugin:
        inject = ["db"]

        async def init(self):
            runs = ctx.db.repo(TestRun)
            await ctx.db.sync_schema()

生命周期语义（DESIGN §4.3，关键）：

* **清理绝不挂 fiber dispose** —— cordis 的 ``fiber.dispose()`` 不区分
  热更新与停机，挂在上面会「改一次配置、丢一次数据」；卸载清理只走
  插件管理器的显式链路（``ctx.plugins.uninstall`` → ``ctx.db.drop_owner``）；
* fiber 卸载的 effect 只负责**排空连接池**（热更新 / 停机都安全：不动表）；
* 启动时交叉核对归属登记与现存插件，孤儿按 ``orphan_policy`` 处理。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context

from .config import DbConfig
from .service import DbService


class DbPlugin:
    """db 插件：依赖 ``ctx.appconfig`` 读 ``[db]`` 节，注册 ``ctx.db`` 服务。"""

    #: 插件名
    name: ClassVar[str] = "db"
    #: 依赖应用配置（读 ``[db]`` 节与 ``[node] install_root``）
    inject: ClassVar[list[str]] = ["appconfig"]
    #: 配置模型（标准 Schema）：驱动校验、默认值注入与写回
    Config: ClassVar[type[DbConfig]] = DbConfig

    def __init__(self, ctx: Context, config: DbConfig | None = None) -> None:
        self.ctx: Context = ctx
        self.config: DbConfig = config if isinstance(config, DbConfig) else DbConfig()
        self.service: DbService = DbService(
            ctx,
            self.config,
            install_root=_install_root(ctx),
            base_dir=_base_dir(ctx),
        )

    async def init(self) -> None:
        """验证连接、建库内元表并执行孤儿回收（失败即启动失败，快速暴露）。"""
        await self.service.start()
        installed = self._installed_plugin_ids()
        orphans = await self.service.sweep_orphans(installed, self.config.orphan_policy)
        if orphans:
            self.ctx.logger.info("孤儿回收完成（%d 个）", len(orphans))

        # 「排空连接池」的清理函数：effect 的**返回值**在 fiber 卸载时被调用
        # （注意是 lambda: self.service.shutdown，不是调用）；只动连接池、不动表。
        self.ctx.fiber.effect(lambda: self.service.shutdown, "plugins.db.shutdown")

    def _installed_plugin_ids(self) -> set[str]:
        """现存插件 id 事实源：``registry.json`` + ``BUILTIN_PLUGINS``（不反向注入 plugin-manager）。"""
        from common.plugins.webapi.plugin_manager import BUILTIN_PLUGINS

        ids = set(BUILTIN_PLUGINS)
        registry_file = _install_root(self.ctx)
        manifest = registry_file / "registry.json" if registry_file is not None else None
        if manifest is not None and manifest.is_file():
            try:
                raw: object = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.ctx.logger.error("插件安装记录无法读取：%s：%s", manifest, exc)
                return ids
            if isinstance(raw, dict):
                ids.update(str(key) for key in cast(dict[object, object], raw))
        return ids


def _install_root(ctx: Context) -> Path | None:
    """从应用配置读插件安装目录（``[node] install_root``）；缺失返回 ``None``。"""
    raw = ctx.appconfig.get("node", "install_root")
    if not raw or not str(raw).strip():
        return None
    return Path(str(raw).strip()).expanduser().resolve()


def _base_dir(ctx: Context) -> Path:
    """节点目录（config.ini 所在目录）：db 相对库路径 / data_dir 的解析基准。

    与 ``install_root`` / ``plugins`` 的相对路径约定一致（按配置文件所在
    目录解析），默认库文件因此落节点目录下的 ``data/``（如 ``master/data/``）。
    """
    loaded = ctx.appconfig.path_loaded
    return loaded.parent if loaded is not None else Path.cwd()


__all__: list[str] = ["DbPlugin"]
