"""test-core 的页面服务（``ctx.test_core_ui``）：「测试任务」页。

任务列表 / 详情页拆给本服务（``inject = ["test_core", "webui"]``），核心不沾
webui：webui 缺席 / 晚到不阻塞核心（同 heartbeat-server 的 ui_service 模式）：

* 依赖自家核心 ``test_core`` 与 UI 宿主 ``webui``，两者就位 cordis 才装载本
  服务并注册页面；``webui`` 缺席时安静 PENDING（长期 pending 不报 ERROR），
  晚出现自动挂载、卸载自动撤回、重载自动重新注册（随依赖纪元流转）；
* 依赖单向「本服务 → 核心」，核心不感知本服务（无注入循环）；本模块不
  import 其他插件包，``webui`` 经 ``ctx.webui`` 服务调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Service


class TestCoreUiService(Service[None]):
    """test-core 页面服务：注册「测试任务」页面（``ui/TasksPage.vue``）。"""

    #: 附属服务标记：随宿主 ``test-core`` 一起呈现，不单列插件
    service_of: ClassVar[str] = "test-core"
    #: 服务名（即 ``ctx.test_core_ui``）
    provide: str | None = "test_core_ui"
    #: fiber 名（诊断用）
    name: str = "test-core_ui"
    #: 依赖集 = 自家核心 ``test_core`` + 页面服务 ``webui``
    inject: ClassVar[list[str]] = ["test_core", "webui"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.test_core_ui
        self.ctx: Context = ctx

    async def init(self) -> object:
        """注册「测试任务」页；返回清理函数（依赖集由 fiber 保证就绪）。"""
        self.ctx.webui.register_ui(
            "tasks",
            kind="page",
            format="vue",
            resource_root=str(Path(__file__).resolve().parent / "ui"),
            entry="TasksPage.vue",
            path="/tasks",
            title="测试任务",
            menu_group="",
            menu_icon="pi pi-list",
            menu_order=98,
        )

        def cleanup() -> None:
            # 注册项绑定调用方 fiber：webui.register_ui 的 fiber effect 随卸载
            # 自动撤销清单条目与静态挂载，无公开的手动注销口；保留清理函数以
            # 统一 init 的 effect 契约，fiber 卸载时与 webui 侧清理幂等共存。
            return None

        return cleanup


__all__: list[str] = ["TestCoreUiService"]
