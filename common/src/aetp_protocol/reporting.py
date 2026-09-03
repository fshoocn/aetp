""" Reporter、Analyzer 和 Notifier 扩展点 DTO。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import ArtifactRef
from .execution import CaseResult, ExecutionResult, ExecutionStatus
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
