"""V2 Requirement、ExecutionPlan、Lease 和执行结果 DTO。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import ArtifactRef, Configuration, ScriptRef, TestCase
from .capabilities import ResourceHealth, SwitchConnection, SwitchRouteAllocation
from .errors import ErrorCode
from .ids import (
    BusinessId,
    JsonObject,
    MessageId,
    PluginId,
    SemVer,
    SessionId,
    Sha256,
    Version,
    VersionConstraint,
    VersionRange,
)
from .plugin_types import PluginDistributionRef


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PluginRequirement(_Strict):
    plugin_id: PluginId
    version: VersionRange


class RuntimeRequirement(_Strict):
    runtime_type: str = Field(min_length=1, max_length=128)
    version: VersionConstraint | None = None


class SoftwareRequirement(_Strict):
    name: str = Field(min_length=1, max_length=128)
    version: VersionConstraint | None = None
    license_required: bool = False


class ResourceRequirement(_Strict):
    resource_type: str = Field(min_length=1, max_length=128)
    quantity: int = Field(default=1, ge=1)
    vendor: str | None = None
    model: str | None = None
    properties: JsonObject = Field(default_factory=dict)
    required_labels: dict[str, str] = Field(default_factory=dict)
    preferred_labels: dict[str, str] = Field(default_factory=dict)
    allow_switching: bool = False


class ExecutionRequirement(_Strict):
    executor: PluginRequirement
    runtimes: tuple[RuntimeRequirement, ...] = ()
    software: tuple[SoftwareRequirement, ...] = ()
    resources: tuple[ResourceRequirement, ...] = ()
    required_tags: tuple[str, ...] = ()


class CancelRequest(_Strict):
    cancel_request_id: MessageId
    run_id: BusinessId
    shard_id: BusinessId
    attempt_no: int = Field(ge=1)
    plan_id: BusinessId
    reason: str = ""


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class RunStatus(StrEnum):
    CREATED = "created"
    DISPATCHED = "dispatched"
    ACKED = "acked"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LOST = "lost"


class AttemptStatus(StrEnum):
    CREATED = "created"
    DISPATCHED = "dispatched"
    ACKED = "acked"
    RUNNING = "running"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LOST = "lost"


class ShardStatus(StrEnum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    WAITING_RECOVERY = "waiting_recovery"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class NodePresenceState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class CaseStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class CaseResult(_Strict):
    case_key: str = Field(min_length=1)
    status: CaseStatus
    duration_ms: int | None = Field(default=None, ge=0)
    error_summary: str | None = None
    detail: JsonObject | None = None


class ExecutionError(_Strict):
    code: ErrorCode
    message: str
    retryable: bool = False


class ExecutionResult(_Strict):
    status: ExecutionStatus
    passed: bool
    case_results: tuple[CaseResult, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    metrics: JsonObject = Field(default_factory=dict)
    data: JsonObject = Field(default_factory=dict)
    error: ExecutionError | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> ExecutionResult:
        if self.status is ExecutionStatus.SUCCEEDED and self.error is not None:
            raise ValueError("succeeded result cannot contain execution error")
        if self.status is not ExecutionStatus.SUCCEEDED and self.passed:
            raise ValueError("non-succeeded result cannot be passed")
        case_keys = tuple(result.case_key for result in self.case_results)
        if len(case_keys) != len(set(case_keys)):
            raise ValueError("case_results.case_key must be unique")
        return self


class RuntimeInfo(_Strict):
    provider_id: str
    runtime_id: str
    runtime_type: str
    version: Version
    executable_ref: str | None = None


class RuntimeRequest(_Strict):
    requirement: RuntimeRequirement
    runtime_id: str | None = None


class RuntimeBinding(_Strict):
    runtime_id: str
    runtime_type: str
    version: Version
    executable_path: str


class RuntimeSelection(_Strict):
    runtime_id: str
    runtime_type: str
    version: Version


class ResourceInfo(_Strict):
    resource_id: BusinessId
    provider_id: str
    resource_type: str
    vendor: str | None = None
    model: str | None = None
    channel: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    properties: JsonObject = Field(default_factory=dict)
    health: ResourceHealth
    switch_connection: SwitchConnection | None = None


class ResourceBinding(_Strict):
    resource_id: BusinessId
    resource_type: str
    labels: dict[str, str] = Field(default_factory=dict)
    switch_route: SwitchRouteAllocation | None = None


class ExecutorRef(_Strict):
    plugin_id: PluginId
    version: SemVer


class LeaseState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class ResourceLease(_Strict):
    lease_id: BusinessId
    run_id: BusinessId
    shard_id: BusinessId
    attempt_id: BusinessId
    node_id: BusinessId
    resource_id: BusinessId
    state: LeaseState
    revision: int = Field(ge=1)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


class PlanResourceBinding(_Strict):
    lease_id: BusinessId
    resource_id: BusinessId
    resource_type: str
    properties: JsonObject = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    lease_revision: int = Field(ge=1)
    expires_at: datetime
    switch_route: SwitchRouteAllocation | None = None


class ExecutionPlan(_Strict):
    schema_version: Literal[2]
    plan_id: BusinessId
    plan_hash: Sha256
    run_id: BusinessId
    task_id: BusinessId
    script_binding_id: BusinessId
    script_definition_id: BusinessId
    shard_id: BusinessId
    attempt_id: BusinessId
    attempt_no: int = Field(ge=1)
    project_id: BusinessId
    node_id: BusinessId
    target_session_id: SessionId
    executor: ExecutorRef
    plugin_package: PluginDistributionRef | None = None
    runtime: RuntimeSelection | None = None
    resource_bindings: tuple[PlanResourceBinding, ...] = ()
    script: ScriptRef
    input_artifacts: tuple[ArtifactRef, ...] = ()
    configuration: Configuration
    execution_parameters: JsonObject = Field(default_factory=dict)
    case_keys: tuple[str, ...]
    artifact_upload_url: str | None = None
    created_at: datetime
    deadline_at: datetime

    @model_validator(mode="after")
    def validate_plan(self) -> ExecutionPlan:
        if self.deadline_at <= self.created_at:
            raise ValueError("deadline_at must be later than created_at")
        if len(self.case_keys) != len(set(self.case_keys)):
            raise ValueError("case_keys must be unique")
        lease_ids = tuple(binding.lease_id.root for binding in self.resource_bindings)
        if len(lease_ids) != len(set(lease_ids)):
            raise ValueError("resource lease_id must be unique")
        return self


class SplitPolicy(_Strict):
    type: Literal["none", "by_time", "by_case_count", "custom"]
    target_count: int | None = Field(default=None, ge=1)
    target_duration_s: int | None = Field(default=None, ge=1)
    plugin_id: PluginId | None = None

    @model_validator(mode="after")
    def validate_policy(self) -> SplitPolicy:
        if self.type == "by_case_count" and self.target_count is None:
            raise ValueError("by_case_count requires target_count")
        if self.type == "by_time" and self.target_duration_s is None:
            raise ValueError("by_time requires target_duration_s")
        if self.type == "custom" and self.plugin_id is None:
            raise ValueError("custom requires plugin_id")
        return self


class RetryPolicy(_Strict):
    max_attempts: int = Field(default=1, ge=1)
    failover_nodes: bool = False
    retry_failed_cases: bool = False
    backoff_initial_s: int = Field(default=1, ge=0)
    backoff_max_s: int = Field(default=60, ge=0)

    @model_validator(mode="after")
    def validate_backoff(self) -> RetryPolicy:
        if self.backoff_max_s < self.backoff_initial_s:
            raise ValueError("backoff_max_s cannot be less than backoff_initial_s")
        return self


class TriggerType(StrEnum):
    MANUAL_WEB = "manual_web"
    API = "api"
    SCHEDULE = "schedule"
    CI_WEBHOOK = "ci_webhook"
    RETRY = "retry"
    RECOVERY = "recovery"


class AdmissionRequest(_Strict):
    run_id: BusinessId
    project_id: BusinessId
    requirement: ExecutionRequirement
    trigger: TriggerType


class AdmissionDecision(_Strict):
    accepted: bool
    code: ErrorCode | None = None
    message: str = ""


class ShardingRequest(_Strict):
    cases: tuple[TestCase, ...]
    policy: SplitPolicy
    configuration: Configuration


class ShardSpec(_Strict):
    shard_index: int = Field(ge=0)
    case_keys: tuple[str, ...]
    execution_parameters: JsonObject = Field(default_factory=dict)


class ShardingResult(_Strict):
    shards: tuple[ShardSpec, ...]


class RuntimeSystemVersion(_Strict):
    version: Version


class ReconcileAttempt(_Strict):
    attempt_id: BusinessId
    plan_id: BusinessId
    plan_hash: Sha256
    state: Literal["running", "succeeded", "failed", "cancelled", "timed_out"]
    last_progress_sequence: int = Field(ge=0)
    result: ExecutionResult | None = None


class ExecutionReconcile(_Strict):
    node_id: BusinessId
    attempts: tuple[ReconcileAttempt, ...] = ()


for model in (RuntimeRequirement, SoftwareRequirement, ResourceInfo, ExecutionPlan, ReconcileAttempt):
    model.model_rebuild()
