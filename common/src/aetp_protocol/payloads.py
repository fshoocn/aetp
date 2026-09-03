"""关键消息载荷 DTO（§8.4 + verify 扩展）。

Pydantic 模型，extra=forbid 拒绝非法字段；Master/Agent 共用同一契约。
本节只定义核心 payload；运行期日志/进度等按需后续补充。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import ArtifactRef
from .capabilities import (
    AgentMaintenanceState,
    NodeCapabilitySnapshot,
    PluginInventoryItem,
)
from .errors import ErrorCode
from .execution import (
    AttemptStatus,
    CancelRequest,
    CaseStatus,
    ExecutionPlan,
    ExecutionReconcile,
    ExecutionResult,
    NodePresenceState,
    ReconcileAttempt,
)
from .ids import BusinessId, JsonObject, PluginId, RequestId, SemVer, SessionId, Sha256, Version
from .logs import AgentLogBatch, LogEvent, LogLevel
from .message_types import MessageType
from .plugins import PluginSyncRequest, PluginSyncResult


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------  message payloads ------------------------------


class NodeLoad(_Strict):
    running_attempts: int = Field(ge=0)
    queued_attempts: int = Field(ge=0)


class NodeRegister(_Strict):
    node_id: BusinessId
    session_id: SessionId
    name: str
    tags: tuple[str, ...] = ()
    capability_snapshot: NodeCapabilitySnapshot


class NodeRegisterAck(_Strict):
    node_id: BusinessId
    session_id: SessionId
    accepted: bool
    code: ErrorCode | None = None
    message: str = ""


class Heartbeat(_Strict):
    node_id: BusinessId
    status: NodePresenceState = NodePresenceState.ONLINE
    maintenance_state: AgentMaintenanceState
    load: NodeLoad
    active_attempt_ids: tuple[BusinessId, ...] = ()
    capability_revision: int = Field(ge=1)


class Presence(_Strict):
    node_id: BusinessId
    reason: str
    occurred_at: datetime


class ExecutionAck(_Strict):
    run_id: BusinessId
    shard_id: BusinessId
    attempt_id: BusinessId
    plan_id: BusinessId
    plan_hash: Sha256
    accepted: bool
    code: ErrorCode | None = None
    message: str = ""

    @model_validator(mode="after")
    def validate_rejection_code(self) -> ExecutionAck:
        if not self.accepted and self.code is None:
            raise ValueError("rejected execution ack must contain code")
        return self


class ExecutionProgress(_Strict):
    run_id: BusinessId
    shard_id: BusinessId
    attempt_id: BusinessId
    plan_id: BusinessId
    sequence: int = Field(ge=1)
    percent: int = Field(ge=0, le=100)
    stage: str
    message: str = ""


class ExecutionFinished(_Strict):
    run_id: BusinessId
    shard_id: BusinessId
    attempt_id: BusinessId
    plan_id: BusinessId
    plan_hash: Sha256
    result: ExecutionResult
    finished_at: datetime


class ExecutionCancel(_Strict):
    request: CancelRequest


class CaseStatusEvent(_Strict):
    run_id: BusinessId
    shard_id: BusinessId
    attempt_id: BusinessId
    plan_id: BusinessId
    case_key: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    status: CaseStatus


class ExecutionLogEntry(_Strict):
    sequence: int = Field(ge=1)
    level: LogLevel
    message: str = Field(min_length=1, max_length=8192)
    detail: JsonObject = Field(default_factory=dict)
    occurred_at: datetime


class ExecutionLogBatch(_Strict):
    run_id: BusinessId
    shard_id: BusinessId
    attempt_id: BusinessId
    plan_id: BusinessId
    first_sequence: int = Field(ge=1)
    entries: tuple[ExecutionLogEntry, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_sequences(self) -> ExecutionLogBatch:
        sequences = tuple(entry.sequence for entry in self.entries)
        if sequences[0] != self.first_sequence or sequences != tuple(sorted(sequences)):
            raise ValueError("log sequences must start at first_sequence and increase")
        if len(sequences) != len(set(sequences)):
            raise ValueError("log sequences must be strictly increasing")
        return self


class LogComplete(_Strict):
    run_id: BusinessId
    shard_id: BusinessId
    attempt_id: BusinessId
    plan_id: BusinessId
    last_sequence: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    artifact_refs: tuple[ArtifactRef, ...] = ()


class LeaseRenewRequest(_Strict):
    plan_id: BusinessId
    attempt_id: BusinessId
    lease_id: BusinessId
    revision: int = Field(ge=1)
    requested_expires_at: datetime


class LeaseRenewed(_Strict):
    plan_id: BusinessId
    attempt_id: BusinessId
    lease_id: BusinessId
    accepted: bool
    revision: int = Field(ge=1)
    expires_at: datetime | None = None
    code: ErrorCode | None = None

    @model_validator(mode="after")
    def validate_rejection_code(self) -> LeaseRenewed:
        if not self.accepted and self.code is None:
            raise ValueError("rejected lease renewal must contain code")
        return self


class ExecutionReconcileResult(_Strict):
    node_id: BusinessId
    accepted: bool
    code: ErrorCode | None = None
    attempts: tuple[ReconcileAttempt, ...] = ()
    message: str = ""

    @model_validator(mode="after")
    def validate_rejection_code(self) -> ExecutionReconcileResult:
        if not self.accepted and self.code is None:
            raise ValueError("rejected execution reconcile result must contain code")
        return self


class MaintenanceStatus(_Strict):
    node_id: BusinessId
    sequence: int = Field(ge=1)
    state: AgentMaintenanceState
    sync_id: BusinessId | None = None
    active_attempt_count: int = Field(ge=0)
    message: str = ""
    occurred_at: datetime


class DiagnosticsRequest(_Strict):
    request_id: RequestId
    node_id: BusinessId
    include_log_tail: bool = True
    log_tail_count: int = Field(default=200, ge=0, le=2000)


class AgentSystemInfo(_Strict):
    hostname: str
    os_name: str
    os_version: str
    process_id: int = Field(ge=0)
    agent_started_at: datetime
    python_version: Version
    cpu_cores: int = Field(ge=0)
    memory_total_mb: int = Field(ge=0)
    memory_available_mb: int = Field(ge=0)
    disk_free_mb: int = Field(ge=0)
    agent_version: SemVer
    protocol_version: int


class MqttConnectionInfo(_Strict):
    connected: bool
    broker_endpoint: str
    last_connected_at: datetime | None = None
    reconnect_count: int = Field(ge=0)
    last_error_code: ErrorCode | None = None
    last_error_message: str | None = None


class ActiveAttemptInfo(_Strict):
    attempt_id: BusinessId
    plan_id: BusinessId
    run_id: BusinessId
    state: AttemptStatus
    started_at: datetime | None = None


class DiagnosticsSnapshot(_Strict):
    request_id: RequestId
    node_id: BusinessId
    collected_at: datetime
    maintenance_state: AgentMaintenanceState
    system: AgentSystemInfo
    mqtt: MqttConnectionInfo
    plugins: tuple[PluginInventoryItem, ...]
    active_attempts: tuple[ActiveAttemptInfo, ...]
    capability_revision: int = Field(ge=1)
    log_tail: tuple[LogEvent, ...] = ()


class AgentLogReceived(_Strict):
    node_id: BusinessId
    session_id: SessionId
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    accepted: bool = True
    code: ErrorCode | None = None
    message: str = ""

    @model_validator(mode="after")
    def validate_rejection_code(self) -> AgentLogReceived:
        if not self.accepted and self.code is None:
            raise ValueError("rejected agent log receipt must contain code")
        return self


class RemoteOperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RemoteOperation(_Strict):
    operation_id: BusinessId
    node_id: BusinessId
    kind: Literal["diagnostics", "plugin_sync", "log_level", "drain", "restart"]
    status: RemoteOperationStatus
    error_code: ErrorCode | None = None
    message: str = ""
    created_at: datetime
    updated_at: datetime


class LogLevelUpdateRequest(_Strict):
    node_id: BusinessId
    operation_id: BusinessId
    expected_session_id: SessionId
    component: str = Field(min_length=1, max_length=128)
    plugin_id: PluginId | None = None
    level: LogLevel
    expires_at: datetime | None = None


class LogLevelUpdateResult(_Strict):
    node_id: BusinessId
    operation_id: BusinessId
    accepted: bool
    level: LogLevel | None = None
    code: ErrorCode | None = None
    message: str = ""

    @model_validator(mode="after")
    def validate_rejection_code(self) -> LogLevelUpdateResult:
        if not self.accepted and self.code is None:
            raise ValueError("rejected log level update must contain code")
        return self


class MaintenanceDrainRequest(_Strict):
    node_id: BusinessId
    operation_id: BusinessId
    expected_session_id: SessionId
    drain_timeout_s: int = Field(ge=0)
    reason: str = ""


class MaintenanceDrainResult(_Strict):
    node_id: BusinessId
    operation_id: BusinessId
    accepted: bool
    active_attempt_count: int = Field(ge=0)
    code: ErrorCode | None = None
    message: str = ""

    @model_validator(mode="after")
    def validate_rejection_code(self) -> MaintenanceDrainResult:
        if not self.accepted and self.code is None:
            raise ValueError("rejected maintenance drain must contain code")
        return self


class MaintenanceRestartRequest(_Strict):
    node_id: BusinessId
    operation_id: BusinessId
    expected_session_id: SessionId
    drain_timeout_s: int = Field(ge=0)
    reason: str = ""


class MaintenanceRestartResult(_Strict):
    node_id: BusinessId
    operation_id: BusinessId
    accepted: bool
    code: ErrorCode | None = None
    message: str = ""

    @model_validator(mode="after")
    def validate_rejection_code(self) -> MaintenanceRestartResult:
        if not self.accepted and self.code is None:
            raise ValueError("rejected maintenance restart must contain code")
        return self


PAYLOAD_MODELS = {
    MessageType.NODE_REGISTER: NodeRegister,
    MessageType.NODE_REGISTER_ACK: NodeRegisterAck,
    MessageType.NODE_CAPABILITY_SNAPSHOT: NodeCapabilitySnapshot,
    MessageType.NODE_HEARTBEAT: Heartbeat,
    MessageType.PRESENCE: Presence,
    MessageType.EXECUTION_PLAN: ExecutionPlan,
    MessageType.EXECUTION_ACK: ExecutionAck,
    MessageType.EXECUTION_CANCEL: ExecutionCancel,
    MessageType.EXECUTION_PROGRESS: ExecutionProgress,
    MessageType.EXECUTION_LOG: ExecutionLogBatch,
    MessageType.EXECUTION_CASE_STATUS: CaseStatusEvent,
    MessageType.EXECUTION_FINISHED: ExecutionFinished,
    MessageType.EXECUTION_LOG_COMPLETE: LogComplete,
    MessageType.LEASE_RENEW: LeaseRenewRequest,
    MessageType.LEASE_RENEWED: LeaseRenewed,
    MessageType.EXECUTION_RECONCILE: ExecutionReconcile,
    MessageType.EXECUTION_RECONCILE_RESULT: ExecutionReconcileResult,
    MessageType.AGENT_PLUGIN_SYNC: PluginSyncRequest,
    MessageType.AGENT_PLUGIN_SYNC_RESULT: PluginSyncResult,
    MessageType.AGENT_MAINTENANCE_STATUS: MaintenanceStatus,
    MessageType.AGENT_DIAGNOSTICS_REQUEST: DiagnosticsRequest,
    MessageType.AGENT_DIAGNOSTICS_SNAPSHOT: DiagnosticsSnapshot,
    MessageType.AGENT_LOG_BATCH: AgentLogBatch,
    MessageType.AGENT_LOG_RECEIVED: AgentLogReceived,
    MessageType.AGENT_LOG_LEVEL_UPDATE: LogLevelUpdateRequest,
    MessageType.AGENT_LOG_LEVEL_UPDATED: LogLevelUpdateResult,
    MessageType.AGENT_MAINTENANCE_DRAIN: MaintenanceDrainRequest,
    MessageType.AGENT_MAINTENANCE_DRAIN_RESULT: MaintenanceDrainResult,
    MessageType.AGENT_MAINTENANCE_RESTART: MaintenanceRestartRequest,
    MessageType.AGENT_MAINTENANCE_RESTART_RESULT: MaintenanceRestartResult,
}


for model in (
    Heartbeat,
    ExecutionCancel,
    ExecutionLogEntry,
    AgentSystemInfo,
    ActiveAttemptInfo,
    DiagnosticsSnapshot,
    LogLevelUpdateRequest,
    LogLevelUpdateResult,
    MaintenanceDrainRequest,
    MaintenanceDrainResult,
    MaintenanceRestartRequest,
    MaintenanceRestartResult,
):
    model.model_rebuild()
