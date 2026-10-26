"""数据库插件入口：装配引擎、建库内元表；界面层独立为 ``db_ui`` 服务。

加载后其他插件声明 ``inject = ["db"]`` 即可建表与增删改查::

    class MyPlugin:
        inject = ["db"]

        async def init(self):
            runs = ctx.db.repo(TestRun)
            await ctx.db.sync_schema()

**两个服务、依赖各归各的**（cordis 一个插件可提供多个服务）：

* ``ctx.db``（DbService）—— 核心数据服务，只依赖 ``appconfig``；
* ``ctx.db_ui``（DbUiService）—— 界面层（自检接口 +「数据库」页面），
  硬依赖 ``db`` + ``webapi``，由本插件一并拉起、依赖缺席时 PENDING。
  db 核心因此**永远不需要** ``inject = ["webui"]``。

生命周期语义（DESIGN §4.3，关键）：

* **清理绝不挂 fiber dispose** —— cordis 的 ``fiber.dispose()`` 不区分
  热更新与停机，挂在上面会「改一次配置、丢一次数据」；卸载清理只走
  插件管理器的显式链路（``ctx.plugins.uninstall`` → ``ctx.db.drop_owner``）；
* fiber 卸载的 effect 只负责**排空连接池**（热更新 / 停机都安全：不动表）；
* **孤儿数据不随启动回收**（启动快照会受装载顺序影响、误判内置插件）——
  改由自检接口手动触发：``ctx.db.self_check()`` / ``POST /api/db/self-check``
  （报告孤儿数据，``policy=drop`` 才清理）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Fiber

from .config import DbConfig
from .service import DbService
from .ui_service import DbUiService


class DbPlugin:
    """db 插件：依赖 ``ctx.appconfig`` 读 ``[db]`` 节，注册 ``ctx.db`` 服务。"""

    #: 插件名（即插件 id）
    name: ClassVar[str] = "db"
    #: 展示名与版本：内置插件没有 plugin.json，清单元数据由插件类属性提供
    display_name: ClassVar[str] = "数据库服务"
    version: ClassVar[str] = "1.0.0"
    #: 依赖应用配置（读 ``[db]`` 节）；界面层的 webapi / webui 依赖在
    #: DbUiService 自己的依赖集里，不进本插件（核心不沾 Web 依赖）
    inject: ClassVar[list[str]] = ["appconfig"]
    #: 配置模型（标准 Schema）：驱动校验、默认值注入与写回
    Config: ClassVar[type[DbConfig]] = DbConfig

    def __init__(self, ctx: Context, config: DbConfig | None = None) -> None:
        self.ctx: Context = ctx
        self.config: DbConfig = config if isinstance(config, DbConfig) else DbConfig()
        self.service: DbService = DbService(
            ctx,
            self.config,
            base_dir=_base_dir(ctx),
        )
        self._ui_fiber: Fiber | None = None

    async def init(self) -> None:
        """验证连接、建库内元表并拉起界面服务（失败即启动失败，快速暴露）。"""
        await self.service.start()
        # 界面层独立成服务：依赖集（db + webapi）就绪才激活，从节点 / 无头
        # 部署没有 webapi 时它安静 PENDING、不影响核心；fiber 挂在本插件
        # 生命周期下，卸载 / 热更自动带走。不能 await——依赖缺席会一直挂起。
        self._ui_fiber = self.ctx.plugin(DbUiService)
        # 「排空连接池」的清理函数：effect 的**返回值**在 fiber 卸载时被调用
        # （注意不是调用）；只动连接池，不动表。
        self.ctx.fiber.effect(self._make_cleanup, "plugins.db.shutdown")

    def _make_cleanup(self) -> Callable[[], Awaitable[None]]:
        async def cleanup() -> None:
            await self.service.shutdown()

        return cleanup


def _base_dir(ctx: Context) -> Path:
    """节点目录（config.ini 所在目录）：db 相对库路径 / data_dir 的解析基准。

    与 ``install_root`` / ``plugins`` 的相对路径约定一致（按配置文件所在
    目录解析），默认库文件因此落节点专属数据目录（如
    ``master/data/plugins/db/``）。
    """
    loaded = ctx.appconfig.path_loaded
    return loaded.parent if loaded is not None else Path.cwd()


__all__: list[str] = ["DbPlugin"]
