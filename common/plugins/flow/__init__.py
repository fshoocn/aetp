"""流程引擎内置插件（``common/plugins/flow``）：链式执行与流程编排核心。

对外契约（类型面在 :mod:`common.flow_contracts`，铁律：插件间只经
``ctx.flow`` 交互）::

    from common.plugins.flow import FlowService

    await ctx.plugin(FlowService)     # 依赖 appconfig / node_kind / node_name / db

    class MyProvider:
        inject = ["flow"]
        async def init(self):
            self.reg = self.ctx.flow.step("report", "html", render, labels={"format": "html"})
            self.ctx.flow.define(TEST_TASK)

Phase 3 已含持久化（事件日志 ``aetp_flow_events`` + runs / steps 投影、重启
恢复、幂等键、保留清理）与 HTTP API（:class:`FlowApiService`，inject
flow + webapi，headless 自然 PENDING）。设计文档：
``docs/链式执行与流程编排设计-2026-10-26.md``。
"""

from __future__ import annotations

from .api import FlowApiService
from .config import FlowConfig
from .service import FlowService
from .store import FlowStore
from .ui_service import FlowUiService

__all__: list[str] = [
    "FlowApiService",
    "FlowConfig",
    "FlowService",
    "FlowStore",
    "FlowUiService",
]
