"""heartbeat-server 的页面服务（``ctx.heartbeat_server_ui``）：「从模块管理」页。

页面层与核心解耦（与 appconfig / db / log 的页面服务同模式）：核心
``HeartBeatServerPlugin`` **不再** ``inject = ["webui"]``、不在 ``init`` 里直接
``register_ui``——真实启动里 webui 就绪晚于心跳核心时，核心会被 webui 卡住、
``ctx.nodes`` 与管理 API 一起迟到。拆出本服务后：

* ``inject = ["nodes", "webui"]`` **注入式挂载**：依赖自家核心服务 ``nodes``
  （核心 ``provide`` 的同一 :class:`~master_plugins.heartbeat.registry.NodesService`
  实例，经 ``ctx.nodes`` 传递，本服务按**服务名** ``nodes`` 声明依赖、不 import
  核心代码）与 UI 宿主 ``webui``；两者就位 cordis 才装载本服务并注册页面；
* ``webui`` 是**可选依赖**：缺席时本服务安静 PENDING（长期 pending 不报
  ERROR），核心（``ctx.nodes`` / 管理 API / 状态扫描）照常 ACTIVE；webui
  晚出现自动挂载、卸载自动撤回、重载自动重新注册（随依赖纪元流转，
  每次恰好一条，不重复）；
* 依赖单向「本服务 → 核心」，核心不感知本服务（无注入循环）；本模块不
  import 其他插件包，``webui`` 经 ``ctx.webui`` 服务调用。

装载方式：心跳插件 ``init`` 里 ``self._ui_fiber = self.ctx.plugin(HeartbeatUiService)``
**不 await**（依赖缺席会一直挂起）；fiber 挂在核心插件生命周期下，卸载 /
热更随父 fiber 自动清理，核心的卸载清理不需要也不应该再 dispose 一次。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from cordis_port import Context, Service


class HeartbeatUiService(Service[None]):
    """heartbeat-server 页面服务：注册「从模块管理」页面（``ui/NodesPage.vue``）。"""

    #: 附属服务标记：本类是 ``heartbeat-server`` 插件的界面层载体，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 heartbeat-server 一起呈现
    service_of: ClassVar[str] = "heartbeat-server"
    #: 服务名（即 ``ctx.heartbeat_server_ui``）
    provide: str | None = "heartbeat_server_ui"
    #: fiber 名（UI 贡献归属由此而来：``heartbeat-server_ui:nodes``；权限层
    #: ``plugin_owner`` 规范化去 ``_ui`` 后缀 → 归属名 ``heartbeat-server``）
    name: str = "heartbeat-server_ui"
    #: 页面层的依赖集 = 自家核心服务 ``nodes`` + UI 宿主 ``webui``：两者就位才
    #: 装载，核心 / webui 换装、重载页面跟着级联重挂；webui 是可选项、只圈在
    #: 页面服务里——缺席时安静 PENDING（这正是 webui 不能进核心 inject 的
    #: 原因）；核心 inject 无 webui、依赖单向，无注入循环
    inject: ClassVar[list[str]] = ["nodes", "webui"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.heartbeat_server_ui
        self.ctx: Context = ctx

    def init(self) -> None:
        """注册「从模块管理」页面（依赖集由 fiber 保证就绪，无需轮询）。

        注册项绑定本 fiber：卸载自动移除清单与静态挂载，webui 卸载 / 重载
        触发的重新装载会自动重新注册（每次恰好一条，不重复）。
        """
        self.ctx.webui.register_ui(
            "nodes",
            kind="page",
            format="vue",
            resource_root=str(Path(__file__).resolve().parent / "ui"),
            entry="NodesPage.vue",
            path="/nodes",
            title="从模块管理",
            menu_group="",
            menu_icon="pi pi-desktop",
            menu_order=99,
        )


__all__: list[str] = ["HeartbeatUiService"]
