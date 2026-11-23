"""executor_pytest 管理端的页面服务（``ctx.executor_pytest_master_ui``）：脚本管理页
pytest 材料 slot 面板 + 测试计划编辑器「下发任务 UI」slot 面板（设计 §6）。

页面层与核心解耦（与 heartbeat / test_core 的页面服务同模式）：核心
``ExecPytestMasterPlugin`` **不再** ``inject = ["webui"]``、不在 ``init`` 里直接
``register_ui``——真实启动里 webui 就绪晚于管理端时，核心会被 webui 卡住。拆出
本服务后：

* ``inject = ["executor_pytest_master", "webui"]`` **注入式挂载**：依赖自家核心
  服务 ``executor_pytest_master``（核心 ``provide`` 的同一
  :class:`~master_plugins.executor_pytest.plugin.ExecPytestMasterPlugin` 实例，
  经 ``ctx.executor_pytest_master`` 传递，本服务按**服务名**声明依赖、不 import
  核心代码）与 UI 宿主 ``webui``；  两者就位 cordis 才装载本服务并注册 UI 贡献；
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
    """executor_pytest 管理端页面服务：注册脚本管理页材料面板与计划编辑器下发任务 UI 面板。"""

    #: 附属服务标记：本类是 ``executor-pytest-master`` 插件的界面层载体，不是独立
    #: 插件——插件清单（builtin_specs）据此不单列，随宿主插件一起呈现
    service_of: ClassVar[str] = "executor-pytest-master"
    #: 服务名（即 ``ctx.executor_pytest_master_ui``）
    provide: str | None = "executor_pytest_master_ui"
    #: fiber 名（UI 贡献归属由此而来：``executor-pytest-master_ui:pytest-materials-panel``；
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
        """注册脚本管理页材料面板 + 测试计划编辑器「下发任务 UI」面板（依赖集由 fiber
        保证就绪，无需轮询）。

        旧「测试材料」（``pytest-materials``，/pytest/materials）与「下发测试任务」
        （``pytest-submit``，/pytest/submit）两页已退役（W2-D）：与本 slot 同批
        注销、不再注册——材料能力整体迁入 ``ui/MaterialsPanel.vue``，面板挂脚本
        管理页 slot ``scripts:pytest``（与能力信封的执行器 id 统一，脚本总览
        tab=pytest）；任务下发并入测试计划编辑器（「测试计划与下发」页，W3 交付）。
        contribution_id 用新 id ``pytest-materials-panel``（旧 id 不可复用：同
        owner 同 id 二次注册抛 ValueError，新旧注册必须同批切换）。tab 并集 =
        slot 后缀 ∪ 能力 id，两侧命名必须一致（用执行器 id 而非插件名），否则
        裂成两个 tab。

        下发任务 UI（设计 §6）：``ui/DispatchPanel.vue`` 挂计划编辑器步骤卡片的
        「下发任务 UI」slot ``plans:dispatch:pytest``（target 用**能力 id** 而非
        插件名，沿 scripts:pytest 教训）；payload/update 契约见面板头注释，面板
        只编辑执行器参数（extra_args/keyword/env/workdir/python），核心管理键
        material_id/selection_id/targets 不出现。contribution_id ``pytest-
        dispatch-panel`` 与材料面板错开 order（10 / 20），同样不可与其他贡献撞键。

        注册项绑定本 fiber：卸载自动移除清单与静态挂载，webui 卸载 / 重载
        触发的重新装载会自动重新注册（每次恰好一条，不重复）。
        """
        ui_root = str(Path(__file__).resolve().parent / "ui")
        self.ctx.webui.register_ui(
            "pytest-materials-panel",
            kind="slot",
            format="vue",
            resource_root=ui_root,
            entry="MaterialsPanel.vue",
            target="scripts:pytest",
            title="pytest 脚本",
            order=10,
        )
        self.ctx.webui.register_ui(
            "pytest-dispatch-panel",
            kind="slot",
            format="vue",
            resource_root=ui_root,
            entry="DispatchPanel.vue",
            target="plans:dispatch:pytest",
            title="pytest 任务参数",
            order=20,
        )


__all__: list[str] = ["ExecPytestUiService"]
