""" Reporter、Analyzer 和 Notifier 扩展点 DTO。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import ArtifactRef
from .execution import (
    CaseResult,
    ExecutionResult,
    ExecutionStatus,
    ShardingRequest,
    ShardingResult,
)
from .ids import BusinessId, JsonObject


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UnifiedTestResult(_Strict):
    """Reporter 归一化后的 Run 结果，不覆盖原始执行事实。"""

    run_id: BusinessId
    status: ExecutionStatus
    passed: bool
    cases: tuple[CaseResult, ...] = ()
    metrics: JsonObject = Field(default_factory=dict)
    data: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_consistency(self) -> UnifiedTestResult:
        if self.status is ExecutionStatus.SUCCEEDED and not self.passed:
            raise ValueError("succeeded unified result must be passed")
        if self.status is not ExecutionStatus.SUCCEEDED and self.passed:
            raise ValueError("non-succeeded unified result cannot be passed")
        case_keys = tuple(case.case_key for case in self.cases)
        if len(case_keys) != len(set(case_keys)):
            raise ValueError("unified result case keys must be unique")
        return self


class ReportRequest(_Strict):
    run_id: BusinessId
    artifacts: tuple[ArtifactRef, ...] = ()
    execution_result: ExecutionResult | None = None


class ReportResult(_Strict):
    result: UnifiedTestResult | None = None
    derived_artifacts: tuple[ArtifactRef, ...] = ()


class AnalysisRequest(_Strict):
    run_id: BusinessId
    result: UnifiedTestResult
    historical_window: int = Field(default=0, ge=0)


class AnalysisResult(_Strict):
    metrics: JsonObject = Field(default_factory=dict)
    derived_artifacts: tuple[ArtifactRef, ...] = ()


class NotificationPolicy(_Strict):
    mode: Literal["immediate", "run_summary", "digest"] = "immediate"
    window_s: int = Field(default=0, ge=0)
    max_items: int = Field(default=1, ge=1)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_mode(self) -> NotificationPolicy:
        if self.mode == "digest" and self.window_s < 1:
            raise ValueError("digest 模式的 window_s 必须大于 0")
        return self


class NotificationRequest(_Strict):
    event_id: BusinessId
    event_type: str = Field(min_length=1, max_length=128)
    project_id: BusinessId
    payload: JsonObject
    policy: NotificationPolicy = Field(default_factory=NotificationPolicy)


class DeliveryStatus(StrEnum):
    """Notifier 统一投递状态。"""

    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    FAILED = "failed"


class DeliveryResult(_Strict):
    status: Literal["succeeded", "retrying", "failed"]
    provider_message_id: str | None = None
    retry_after_s: int | None = Field(default=None, ge=0)
    error: str | None = None


class PluginContext(Protocol):
    async def read_artifact(self, artifact: ArtifactRef) -> bytes: ...


class ReporterPlugin(Protocol):
    async def report(self, request: ReportRequest, context: PluginContext) -> ReportResult: ...


class AnalyzerPlugin(Protocol):
    async def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult: ...


class NotifierPlugin(Protocol):
    async def send(self, request: NotificationRequest, context: PluginContext) -> DeliveryResult: ...


class NotificationDelivery(_Strict):
    """通知渠道插件的一次投递载荷（协议化，不含 Kernel 内部类型）。

    ``endpoint_config`` 是端点脱敏配置的 JSON 值（如 webhook url），不含密钥；
    密钥由 Kernel 以 ``secret_value`` 单独传入。
    """

    channel_type: str
    subject: str = ""
    body: str = ""
    severity: str = "info"
    endpoint_config: JsonObject = Field(default_factory=dict)
    event_id: str | None = None
    event_type: str | None = None
    project_id: str | None = None
    payload: JsonObject = Field(default_factory=dict)


class NotifierChannel(Protocol):
    """通知渠道插件（point=notifier）：一个渠道对应一个 channel_type。

    插件工厂返回带 ``channel_type`` 与 ``async deliver(delivery, secret_value)``
    的对象；Kernel 用适配器桥接成内部 ``NotificationSender`` 注册进
    ``SenderRegistry``，供 ``NotificationDispatcher`` 使用。
    """

    channel_type: str

    async def deliver(
        self,
        delivery: NotificationDelivery,
        secret_value: str | None = None,
    ) -> DeliveryResult: ...


class ShardingPlugin(Protocol):
    """自定义分片插件：把一次运行的 cases 按 policy 拆成多个 Shard。

    只在 ``SplitPolicy.type == "custom"`` 时被 Kernel 调用；插件不得直接写 Run/Shard
    状态，只返回纯分片结果。
    """

    def split(self, request: ShardingRequest) -> ShardingResult: ...


__all__ = [
    "AnalysisRequest",
    "AnalysisResult",
    "DeliveryResult",
    "DeliveryStatus",
    "NotificationPolicy",
    "NotificationRequest",
    "PluginContext",
    "ReporterPlugin",
    "ReportRequest",
    "ReportResult",
    "AnalyzerPlugin",
    "NotifierPlugin",
    "UnifiedTestResult",
]
