"""log 的页面服务（``ctx.log_ui``，``provide = "log_ui"``）：「日志」查看页。

日志核心（LogPlugin，``ctx.log``）必须足够早装载以捕获装配期日志，不沾 Web
依赖；查看页独立成本服务、``inject = ["log", "webui"]`` **注入式挂载**——
依赖自家核心服务 ``log`` 与 UI 宿主 ``webui``：两者就位 cordis 才装载本服务
并注册页面（核心没就位页面不出现），核心 / webui 换装、重载时本 fiber 随
依赖纪元自动卸载重装、重新注册，不需要就绪守护轮询。

``webui`` 是**可选依赖**（从节点没有 webui）：缺席时本服务安静 PENDING，
日志出口 / 入库 / 查询 API 都不受影响。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Service


class LogUiService(Service[None]):
    """log 页面服务：注册「日志」页面（``ui/LogsPage.vue``）。"""

    #: 附属服务标记：本类是 ``log`` 插件的界面层载体，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 log 一起呈现
    service_of: ClassVar[str] = "log"
    #: 服务名（即 ``ctx.log_ui``）
    provide: str | None = "log_ui"
    #: fiber 名（webui 贡献归属由此而来：``log_ui:logs``）
    name: str = "log_ui"
    #: 页面层的依赖集 = 自家核心 ``log`` + UI 宿主 ``webui``：两者就位才装载，
    #: 核心换装 / 重载页面跟着级联重挂；从节点缺 webui 时安静 PENDING
    #: （核心因此不沾 webui 依赖）
    inject: ClassVar[list[str]] = ["log", "webui"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.log_ui
        self.ctx: Context = ctx

    def init(self) -> None:
        """注册「日志」页面（依赖集由 fiber 保证就绪，无需轮询）。

        注册项绑定本 fiber：卸载自动移除清单与静态挂载，webui 换装 / 重载
        触发的重新装载会自动重新注册。
        """
        self.ctx.webui.register_ui(
            "logs",
            kind="page",
            format="vue",
            resource_root=str(Path(__file__).resolve().parent / "ui"),
            entry="LogsPage.vue",
            path="/logs",
            title="日志",
            menu_group="",
            menu_order=100,
        )


__all__: list[str] = ["LogUiService"]
