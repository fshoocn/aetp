"""executor_pytest 管理端的页面服务（``ctx.executor_pytest_master_ui``）：材料与下发页。

页面层与核心解耦（与 heartbeat / test_core 的页面服务同模式）：核心
``ExecPytestMasterPlugin`` **不再** ``inject = ["webui"]``、不在 ``init`` 里直接
``register_ui``——真实启动里 webui 就绪晚于管理端时，核心会被 webui 卡住。拆出
本服务后：

* ``inject = ["executor_pytest_master", "webui"]`` **注入式挂载**：依赖自家核心
  服务 ``executor_pytest_master``（核心 ``provide`` 的同一
  :class:`~master_plugins.executor_pytest.plugin.ExecPytestMasterPlugin` 实例，
  经 ``ctx.executor_pytest_master`` 传递，本服务按**服务名**声明依赖、不 import
  核心代码）与 UI 宿主 ``webui``；两者就位 cordis 才装载本服务并注册页面；
* ``webui`` 是**可选依赖**：缺席时本服务安静 PENDING（长期 pending 不报
  ERROR），核心（预推 / inspect 编排 / 管理 API）照常 ACTIVE；webui 晚出现自动
  挂载、卸载自动撤回、重载自动重新注册（随依赖纪元流转，每次恰好一条，不重复）；
* 依赖单向「本服务 → 核心」，核心不感知本服务（无注入循环）；本模块不 import
  其他插件包，``webui`` 经 ``ctx.webui`` 服务调用。

装载方式：管理端插件 ``init`` 里 ``self._ui_fiber = self.ctx.plugin(ExecPytestUiService)``
**不 await**（依赖缺席会一直挂起）；fiber 挂在核心插件生命周期下，卸载 / 热更
随父 fiber 自动清理，核心的卸载清理不需要也不应该再 dispose 一次。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Service


class ExecPytestUiService(Service[None]):
    """executor_pytest 管理端页面服务：注册「测试材料」与「下发测试任务」两页。"""

    #: 附属服务标记：本类是 ``executor-pytest-master`` 插件的界面层载体，不是独立
    #: 插件——插件清单（builtin_specs）据此不单列，随宿主插件一起呈现
    service_of: ClassVar[str] = "executor-pytest-master"
    #: 服务名（即 ``ctx.executor_pytest_master_ui``）
    provide: str | None = "executor_pytest_master_ui"
    #: fiber 名（UI 贡献归属由此而来：``executor-pytest-master_ui:pytest-materials``；
    #: 权限层 ``plugin_owner`` 规范化去 ``_ui`` 后缀 → 归属名 ``executor-pytest-master``）
    name: str = "executor-pytest-master_ui"
    #: 页面层的依赖集 = 自家核心服务 ``executor_pytest_master`` + UI 宿主 ``webui``：
    #: 两者就位才装载，核心 / webui 换装、重载页面跟着级联重挂；webui 是可选项、
    #: 只圈在页面服务里——缺席时安静 PENDING（这正是 webui 不能进核心 inject 的
    #: 原因）；核心 inject 无 webui、依赖单向，无注入循环
    inject: ClassVar[list[str]] = ["executor_pytest_master", "webui"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.executor_pytest_master_ui
        self.ctx: Context = ctx

    def init(self) -> None:
        """注册「测试材料」「下发测试任务」两页（依赖集由 fiber 保证就绪，无需轮询）。

        注册项绑定本 fiber：卸载自动移除清单与静态挂载，webui 卸载 / 重载
        触发的重新装载会自动重新注册（每次恰好一条，不重复）。
        """
        ui_root = str(Path(__file__).resolve().parent / "ui")
        self.ctx.webui.register_ui(
            "pytest-materials",
            kind="page",
            format="vue",
            resource_root=ui_root,
            entry="MaterialsPage.vue",
            path="/pytest/materials",
            title="测试材料",
            menu_group="",
            menu_icon="pi pi-folder",
            menu_order=96,
        )
        self.ctx.webui.register_ui(
            "pytest-submit",
            kind="page",
            format="vue",
            resource_root=ui_root,
            entry="SubmitPage.vue",
            path="/pytest/submit",
            title="下发测试任务",
            menu_group="",
            menu_icon="pi pi-send",
            menu_order=97,
        )


__all__: list[str] = ["ExecPytestUiService"]
