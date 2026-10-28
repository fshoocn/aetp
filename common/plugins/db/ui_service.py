"""db 的页面服务（``ctx.db_ui``，``provide = "db_ui"``）：「数据库」Vue 页面。

db 插件的界面层拆成两个服务、依赖各归各的，核心永远不沾 Web 依赖：

* ``ctx.db_api``（:class:`~common.plugins.db.api_service.DbApiService`）——
  API 层：``POST /api/db/self-check``，硬依赖 ``db`` + ``webapi``；
* ``ctx.db_ui``（本服务）—— 页面层：``inject = ["db", "webui"]``，
  **注入式挂载**——依赖自家核心服务 ``db`` 与 UI 宿主 ``webui``：两者就位
  cordis 才装载本服务并注册页面（核心没就位页面不出现），核心 / webui
  换装、重载时本 fiber 随依赖纪元自动卸载重装、重新注册，不需要就绪守护
  轮询。

``webui`` 是**可选依赖**（从节点没有 webui），只圈在页面服务里：缺席时
本服务安静 PENDING，``ctx.db`` 与自检接口都不受影响——db 核心因此
**永远不需要** ``inject = ["webui"]``。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Service


class DbUiService(Service[None]):
    """db 页面服务：注册「数据库」页面（``ui/DbPage.vue``）。"""

    #: 附属服务标记：本类是 ``db`` 插件的界面层载体之一，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 db 一起呈现
    service_of: ClassVar[str] = "db"
    #: 服务名（即 ``ctx.db_ui``）
    provide: str | None = "db_ui"
    #: fiber 名（webui 贡献归属由此而来：``db_ui:db``）
    name: str = "db_ui"
    #: 页面层的依赖集 = 自家核心 ``db`` + UI 宿主 ``webui``：两者就位才装载，
    #: 核心换装 / 重载页面跟着级联重挂；webui 是可选依赖，圈在页面服务里
    #: 即可——从节点缺 webui 时安静 PENDING（这正是 webui 不能进核心
    #: ``inject`` 的原因）
    inject: ClassVar[list[str]] = ["db", "webui"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.db_ui
        self.ctx: Context = ctx

    def init(self) -> None:
        """注册「数据库」页面（依赖集由 fiber 保证就绪，无需轮询）。

        注册项绑定本 fiber：卸载自动移除清单与静态挂载，webui 换装 / 重载
        触发的重新装载会自动重新注册。
        """
        self.ctx.webui.register_ui(
            "db",
            kind="page",
            format="vue",
            resource_root=str(Path(__file__).resolve().parent / "ui"),
            entry="DbPage.vue",
            path="/db",
            title="数据库",
            menu_group="",
            menu_icon="pi pi-database",
            menu_order=101,
        )


__all__: list[str] = ["DbUiService"]
