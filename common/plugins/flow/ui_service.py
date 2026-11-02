"""flow 的页面服务（``ctx.flow_ui``，``provide = "flow_ui"``）：「流程」Vue 页面。

「页面即服务」约定（db_ui / log_ui 同款）：``inject = ["flow", "webui"]``
注入式挂载——自家核心 ``flow`` 与 UI 宿主 ``webui`` 两者就位才装载并注册
页面；核心 / webui 换装、重载随依赖纪元自动卸载重挂，无需就绪守护轮询。
``webui`` 是软依赖（从节点 / 无头没有）：缺席时本服务安静 PENDING，
``ctx.flow`` 与 ``flow_api`` 都不受影响。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Service


class FlowUiService(Service[None]):
    """flow 页面服务：注册「流程」页面（``ui/FlowPage.vue``）。"""

    #: 附属服务标记：随宿主插件 flow 一起呈现，不单列插件清单
    service_of: ClassVar[str] = "flow"
    #: 服务名（即 ``ctx.flow_ui``）
    provide: str | None = "flow_ui"
    #: fiber 名（webui 贡献归属：``flow_ui:flow``）
    name: str = "flow_ui"
    #: 页面层依赖集 = 自家核心 ``flow`` + UI 宿主 ``webui``
    inject: ClassVar[list[str]] = ["flow", "webui"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.flow_ui
        self.ctx: Context = ctx

    def init(self) -> None:
        """注册「流程」页面（依赖集由 fiber 保证就绪；注册项绑本 fiber）。"""
        self.ctx.webui.register_ui(
            "flow",
            kind="page",
            format="vue",
            resource_root=str(Path(__file__).resolve().parent / "ui"),
            entry="FlowPage.vue",
            path="/flow",
            title="流程",
            menu_group="",
            menu_icon="pi pi-sitemap",
            menu_order=102,
        )


__all__: list[str] = ["FlowUiService"]
