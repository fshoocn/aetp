"""Agent V2 能力快照和诊断消息发布器。"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import sys
from collections.abc import Callable
from ctypes import Structure, byref, c_ulong, c_ulonglong, sizeof
from datetime import UTC, datetime
from pathlib import Path

from aetp_protocol.capabilities import AgentMaintenanceState, NodeCapabilities, NodeCapabilitySnapshot
from aetp_protocol.envelope import SenderKind
from aetp_protocol.ids import BusinessId, MessageId, SemVer, SessionId, TraceId, Version, new_id, stable_id
from aetp_protocol.logs import LogEvent
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    ActiveAttemptInfo,
    AgentSystemInfo,
    CaseStatusEvent,
    DiagnosticsRequest,
    DiagnosticsSnapshot,
    ExecutionAck,
    ExecutionFinished,
    ExecutionLogBatch,
    ExecutionProgress,
    ExecutionReconcile,
    LeaseRenewRequest,
    LogComplete,
    MaintenanceStatus,
    MqttConnectionInfo,
    NodeRegister,
    NodeRegisterAck,
)
from aetp_protocol.plugins import PluginSyncResult
from aetp_protocol.topics import (
    parse_v2_topic,
    v2_command_topic,
    v2_event_topic,
    validate_message_type_for_v2_topic,
    validate_sender_for_v2_topic,
)
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from agent.application.services.capability_snapshot_service import (
    AgentCapabilitySnapshotService,
    CapabilityRevisionCache,
)
from agent.config import AgentSettings
from agent.domain.ledger import Ledger
from agent.plugins.v2_registry import AgentV2PluginRegistry
from common.transport import MqttMessage, Transport


class AgentV2CapabilityPublisher:
    """发布 V2 能力快照和响应诊断请求。"""

    def __init__(
        self,
        transport: Transport,
        settings: AgentSettings,
        registry: AgentV2PluginRegistry,
        *,
        tags: tuple[str, ...] = (),
        capability_scanner: Callable[[], NodeCapabilities] | None = None,
        active_attempts: Callable[[], tuple[ActiveAttemptInfo, ...]] | None = None,
        log_tail: Callable[[int], tuple[LogEvent, ...]] | None = None,
        mqtt_info: Callable[[], MqttConnectionInfo] | None = None,
        agent_version: SemVer | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._registry = registry
        self._tags = tags
        self._capability_scanner = capability_scanner
        self._active_attempts = active_attempts or (lambda: ())
        self._log_tail = log_tail or (lambda _count: ())
        self._mqtt_info = mqtt_info or self._default_mqtt_info
        self._agent_version = agent_version or SemVer("2.0.0")
        self._started_at = started_at or datetime.now(UTC)
        self._revision_cache = CapabilityRevisionCache()
        self._maintenance_state = AgentMaintenanceState.IDLE
        self._v2_registered = False
        self._pending_register_message_id: MessageId | None = None
        self._maintenance_sequence = 0

    async def publish_execution_ack(
        self,
        ack: ExecutionAck,
        session_id: SessionId,
        *,
        correlation_id: MessageId | None = None,
    ) -> None:
        """发布 execution.ack。"""
        await self._publish(
            MessageType.EXECUTION_ACK,
            ack,
            session_id,
            correlation_id=correlation_id,
        )

    def enqueue_lease_renew(
        self,
        ledger: Ledger,
        request: LeaseRenewRequest,
        session_id: SessionId,
    ) -> MessageId:
        """将 lease.renew 写入可靠 Agent outbox，并返回消息 ID。"""
        envelope = self._build_envelope(
            MessageType.LEASE_RENEW,
            request,
            session_id,
        )
        ledger.replace_outbox(
            f"v2-lease-renew:{request.plan_id.root}:{request.lease_id.root}:{request.revision}",
            v2_event_topic(self._node_id().root, "lease.renew"),
            envelope.model_dump(mode="json"),
        )
        return envelope.message_id

    def enqueue_execution_ack(
        self,
        ledger: Ledger,
        ack: ExecutionAck,
        session_id: SessionId,
        *,
        correlation_id: MessageId | None = None,
    ) -> str:
        """将 execution.ack 写入可靠 outbox。"""
        envelope = self._build_envelope(
            MessageType.EXECUTION_ACK,
            ack,
            session_id,
            correlation_id=correlation_id,
        )
        outbox_id = f"v2-execution-ack:{ack.plan_id.root}"
        ledger.replace_outbox(
            outbox_id,
            v2_event_topic(self._node_id().root, "execution.ack"),
            envelope.model_dump(mode="json"),
        )
        return outbox_id

    def enqueue_execution_finished(
        self,
        ledger: Ledger,
        finished: ExecutionFinished,
        session_id: SessionId,
        *,
        correlation_id: MessageId | None = None,
    ) -> str:
        """将 execution.finished 写入可靠 Agent outbox。"""
        envelope = self._build_envelope(
            MessageType.EXECUTION_FINISHED,
            finished,
            session_id,
            correlation_id=correlation_id,
        )
        outbox_id = f"v2-execution-finished:{finished.plan_id.root}"
        ledger.replace_outbox(
            outbox_id,
            v2_event_topic(self._node_id().root, "execution.finished"),
            envelope.model_dump(mode="json"),
        )
        return outbox_id

    def enqueue_execution_reconcile(
        self,
        ledger: Ledger,
        reconcile: ExecutionReconcile,
        session_id: SessionId,
    ) -> str:
        """将重连对账事件写入可靠 Agent outbox。"""
        envelope = self._build_envelope(
            MessageType.EXECUTION_RECONCILE,
            reconcile,
            session_id,
        )
        outbox_id = f"v2-execution-reconcile:{self._node_id().root}:{session_id.root}"
        ledger.replace_outbox(
            outbox_id,
            v2_event_topic(self._node_id().root, "execution.reconcile"),
            envelope.model_dump(mode="json"),
        )
        return outbox_id

    def enqueue_execution_progress(
        self,
        ledger: Ledger,
        progress: ExecutionProgress,
        session_id: SessionId,
    ) -> str:
        """将 V2 execution.progress 写入可靠 Agent outbox。"""
        return self._enqueue_execution_event(
            ledger,
            MessageType.EXECUTION_PROGRESS,
            progress,
            session_id,
            f"execution-progress:{progress.attempt_id.root}:{progress.sequence}",
        )

    def enqueue_execution_log(
        self,
        ledger: Ledger,
        batch: ExecutionLogBatch,
        session_id: SessionId,
    ) -> str:
        """将 V2 execution.log 写入可靠 Agent outbox。"""
        return self._enqueue_execution_event(
            ledger,
            MessageType.EXECUTION_LOG,
            batch,
            session_id,
            f"execution-log:{batch.attempt_id.root}:{batch.first_sequence}",
        )

    def enqueue_execution_case_status(
        self,
        ledger: Ledger,
        event: CaseStatusEvent,
        session_id: SessionId,
    ) -> str:
        """将 V2 execution.case_status 写入可靠 Agent outbox。"""
        return self._enqueue_execution_event(
            ledger,
            MessageType.EXECUTION_CASE_STATUS,
            event,
            session_id,
            f"execution-case:{event.attempt_id.root}:{event.case_key}:{event.sequence}",
        )

    def enqueue_execution_log_complete(
        self,
        ledger: Ledger,
        complete: LogComplete,
        session_id: SessionId,
    ) -> str:
        """将 V2 execution.log_complete 写入可靠 Agent outbox。"""
        return self._enqueue_execution_event(
            ledger,
            MessageType.EXECUTION_LOG_COMPLETE,
            complete,
            session_id,
            f"execution-log-complete:{complete.attempt_id.root}",
        )

    def _enqueue_execution_event(
        self,
        ledger: Ledger,
        message_type: MessageType,
        payload: ExecutionProgress | ExecutionLogBatch | CaseStatusEvent | LogComplete,
        session_id: SessionId,
        logical_key: str,
    ) -> str:
        envelope = self._build_envelope(message_type, payload, session_id)
        outbox_id = stable_id(logical_key).root
        ledger.replace_outbox(
            outbox_id,
            v2_event_topic(self._node_id().root, self._message_segment(message_type)),
            envelope.model_dump(mode="json"),
        )
        return outbox_id

    @property
    def v2_registered(self) -> bool:
        """是否收到当前 session 的 V2 注册 ACK。"""
        return self._v2_registered

    @property
    def pending_register_message_id(self) -> MessageId | None:
        """当前 V2 注册消息 ID，ACK 必须通过 correlation_id 关联。"""
        return self._pending_register_message_id

    def reset_session(self) -> None:
        """切换 MQTT session 时清除旧的 V2 注册状态。"""
        self._v2_registered = False
        self._pending_register_message_id = None

    def set_maintenance_state(self, state: AgentMaintenanceState) -> None:
        """更新后续能力快照和诊断中的维护状态。"""
        self._maintenance_state = state

    def register_ack_topic(self) -> str:
        """返回本节点 V2 注册 ACK 主题。"""
        return v2_command_topic(self._node_id().root, "register.ack")

    def enqueue_register(self, ledger: Ledger, session_id: SessionId) -> str:
        """把带完整能力快照的 V2 注册写入 Agent outbox。"""
        snapshot = self.build_snapshot(session_id)
        payload = NodeRegister(
            node_id=self._node_id(),
            session_id=session_id,
            name=self._settings.name,
            tags=self._tags,
            capability_snapshot=snapshot,
        )
        envelope = self._build_envelope(MessageType.NODE_REGISTER, payload, session_id)
        self._pending_register_message_id = envelope.message_id
        self._v2_registered = False
        ledger.replace_outbox(
            f"v2-register:{self._node_id().root}",
            v2_event_topic(self._node_id().root, "register"),
            envelope.model_dump(mode="json"),
        )
        return envelope.message_id.root

    def handle_register_ack(self, message: MqttMessage, session_id: SessionId) -> bool:
        """校验 V2 注册 ACK 并更新当前 session 注册状态。"""
        try:
            topic_info = parse_v2_topic(message.topic)
            if (
                topic_info.direction != "commands"
                or topic_info.node_id != self._node_id().root
                or topic_info.segment != "register.ack"
            ):
                return False
            envelope = V2Envelope.model_validate(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_v2_topic(message.topic, envelope.sender)
            validate_message_type_for_v2_topic(message.topic, MessageType(envelope.message_type))
            payload = envelope.parse_payload()
            if not isinstance(payload, NodeRegisterAck):
                return False
            if envelope.sender.id != stable_id(self._settings.master_id):
                return False
            if envelope.correlation_id != self._pending_register_message_id:
                return False
            if payload.node_id != self._node_id() or payload.session_id != session_id:
                return False
            self._v2_registered = payload.accepted
            return True
        except Exception:
            return False

    def build_snapshot(self, session_id: SessionId) -> NodeCapabilitySnapshot:
        """生成当前 session 的下一版能力快照。"""
        service = AgentCapabilitySnapshotService(
            node_id=self._node_id(),
            session_id=session_id,
            registry=self._registry,
            tags=self._tags,
            maintenance_state=self._maintenance_state,
            capability_scanner=self._capability_scanner,
            revision_cache=self._revision_cache,
        )
        return service.build_snapshot()

    async def publish_snapshot(self, session_id: SessionId) -> NodeCapabilitySnapshot:
        """发布 node.capability.snapshot 事件并返回快照。"""
        snapshot = self.build_snapshot(session_id)
        await self._publish(MessageType.NODE_CAPABILITY_SNAPSHOT, snapshot, session_id)
        return snapshot

    def collect_diagnostics(
        self,
        request: DiagnosticsRequest,
        session_id: SessionId,
    ) -> DiagnosticsSnapshot:
        """采集当前节点诊断快照。"""
        if request.node_id != self._node_id():
            raise ValueError("诊断请求节点与 Agent 不一致")
        capability_snapshot = self.build_snapshot(session_id)
        return DiagnosticsSnapshot(
            request_id=request.request_id,
            node_id=self._node_id(),
            collected_at=datetime.now(UTC),
            maintenance_state=self._maintenance_state,
            system=self._system_info(capability_snapshot),
            mqtt=self._mqtt_info(),
            plugins=capability_snapshot.plugin_inventory,
            active_attempts=self._active_attempts(),
            capability_revision=capability_snapshot.revision,
            log_tail=(
                self._log_tail(request.log_tail_count)
                if request.include_log_tail
                else ()
            ),
        )

    async def publish_diagnostics(
        self,
        request: DiagnosticsRequest,
        session_id: SessionId,
        *,
        correlation_id: MessageId | None = None,
    ) -> DiagnosticsSnapshot:
        """采集并发布 agent.diagnostics.snapshot。"""
        snapshot = self.collect_diagnostics(request, session_id)
        await self._publish(
            MessageType.AGENT_DIAGNOSTICS_SNAPSHOT,
            snapshot,
            session_id,
            correlation_id=correlation_id,
        )
        return snapshot

    async def publish_plugin_sync_result(
        self,
        result: PluginSyncResult,
        session_id: SessionId,
        *,
        correlation_id: MessageId | None = None,
    ) -> None:
        """发布插件同步逐项结果。"""
        await self._publish(
            MessageType.AGENT_PLUGIN_SYNC_RESULT,
            result,
            session_id,
            correlation_id=correlation_id,
        )

    async def publish_maintenance_status(
        self,
        state: AgentMaintenanceState,
        session_id: SessionId,
        *,
        sync_id: BusinessId | None = None,
        active_attempt_count: int | None = None,
        message: str = "",
        correlation_id: MessageId | None = None,
    ) -> MaintenanceStatus:
        """发布带单调 sequence 的 Agent 维护状态。"""
        self.set_maintenance_state(state)
        self._maintenance_sequence += 1
        status = MaintenanceStatus(
            node_id=self._node_id(),
            sequence=self._maintenance_sequence,
            state=state,
            sync_id=sync_id,
            active_attempt_count=(
                len(self._active_attempts()) if active_attempt_count is None else active_attempt_count
            ),
            message=message,
            occurred_at=datetime.now(UTC),
        )
        await self._publish(
            MessageType.AGENT_MAINTENANCE_STATUS,
            status,
            session_id,
            correlation_id=correlation_id,
        )
        return status

    async def handle_diagnostics_request(
        self,
        message: MqttMessage,
        session_id: SessionId,
    ) -> bool:
        """解析 V2 诊断请求并发布快照；非目标消息返回 False。"""
        try:
            topic_info = parse_v2_topic(message.topic)
            if topic_info.direction != "commands" or topic_info.node_id != self._node_id().root:
                return False
            envelope = V2Envelope.model_validate(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_v2_topic(message.topic, envelope.sender)
            validate_message_type_for_v2_topic(
                message.topic,
                MessageType(envelope.message_type),
            )
            if envelope.message_type != MessageType.AGENT_DIAGNOSTICS_REQUEST.value:
                return False
            if envelope.sender.kind != SenderKind.MASTER:
                return False
            if envelope.sender.id != stable_id(self._settings.master_id):
                return False
            request = envelope.parse_payload()
            if not isinstance(request, DiagnosticsRequest):
                return False
            await self.publish_diagnostics(
                request,
                session_id,
                correlation_id=envelope.message_id,
            )
            return True
        except Exception:
            return False

    def diagnostics_command_topic(self) -> str:
        """返回本节点诊断请求订阅主题。"""
        return v2_command_topic(self._node_id().root, "agent.diagnostics.request")

    async def _publish(
        self,
        message_type: MessageType,
        payload: (
            NodeRegister
            | NodeCapabilitySnapshot
            | DiagnosticsSnapshot
            | ExecutionAck
            | ExecutionFinished
            | ExecutionLogBatch
            | ExecutionProgress
            | CaseStatusEvent
            | ExecutionReconcile
            | LogComplete
            | LeaseRenewRequest
            | PluginSyncResult
            | MaintenanceStatus
        ),
        session_id: SessionId,
        correlation_id: MessageId | None = None,
    ) -> None:
        envelope = self._build_envelope(message_type, payload, session_id, correlation_id=correlation_id)
        await self._transport.publish(
            v2_event_topic(self._node_id().root, self._message_segment(message_type)),
            envelope.model_dump_json().encode("utf-8"),
            qos=1,
        )

    def _node_id(self) -> BusinessId:
        try:
            return BusinessId(self._settings.node_id)
        except ValueError as exc:
            raise ValueError("V2 Agent node_id 必须是 BusinessId") from exc

    def _build_envelope(
        self,
        message_type: MessageType,
        payload: (
            NodeRegister
            | NodeCapabilitySnapshot
            | DiagnosticsSnapshot
            | ExecutionAck
            | ExecutionFinished
            | ExecutionReconcile
            | ExecutionLogBatch
            | ExecutionProgress
            | CaseStatusEvent
            | LogComplete
            | LeaseRenewRequest
            | PluginSyncResult
            | MaintenanceStatus
        ),
        session_id: SessionId,
        *,
        correlation_id: MessageId | None = None,
    ) -> V2Envelope:
        return V2Envelope(
            message_id=MessageId(new_id()),
            correlation_id=correlation_id,
            sent_at=datetime.now(UTC),
            sender=V2Sender(
                kind="agent",
                id=self._node_id(),
                session_id=session_id,
            ),
            message_type=message_type.value,
            trace_id=TraceId(new_id()),
            payload=payload.model_dump(mode="json"),
        )

    @staticmethod
    def _message_segment(message_type: MessageType) -> str:
        return {
            MessageType.NODE_REGISTER: "register",
            MessageType.NODE_CAPABILITY_SNAPSHOT: "capability.snapshot",
            MessageType.AGENT_DIAGNOSTICS_SNAPSHOT: "agent.diagnostics.snapshot",
            MessageType.EXECUTION_ACK: "execution.ack",
            MessageType.EXECUTION_FINISHED: "execution.finished",
            MessageType.EXECUTION_RECONCILE: "execution.reconcile",
            MessageType.EXECUTION_PROGRESS: "execution.progress",
            MessageType.EXECUTION_LOG: "execution.log",
            MessageType.EXECUTION_CASE_STATUS: "execution.case_status",
            MessageType.EXECUTION_LOG_COMPLETE: "execution.log_complete",
            MessageType.LEASE_RENEW: "lease.renew",
            MessageType.AGENT_PLUGIN_SYNC_RESULT: "agent.plugin.sync.result",
            MessageType.AGENT_MAINTENANCE_STATUS: "agent.maintenance.status",
        }[message_type]

    def _system_info(self, snapshot: NodeCapabilitySnapshot) -> AgentSystemInfo:
        system = snapshot.system
        memory_total = system.memory_mb if system is not None and system.memory_mb is not None else 0
        cpu_cores = system.cpu_cores if system is not None and system.cpu_cores is not None else (os.cpu_count() or 0)
        plugin_dir = Path(self._settings.plugin_dir).resolve()
        disk_path = plugin_dir if plugin_dir.exists() else plugin_dir.parent
        disk_free = shutil.disk_usage(disk_path).free // (1024 * 1024)
        return AgentSystemInfo(
            hostname=platform.node(),
            os_name=platform.system() or "unknown",
            os_version=platform.version() or platform.release() or "unknown",
            process_id=os.getpid(),
            agent_started_at=self._started_at,
            python_version=Version(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"),
            cpu_cores=cpu_cores,
            memory_total_mb=memory_total,
            memory_available_mb=_available_memory_mb(),
            disk_free_mb=disk_free,
            agent_version=self._agent_version,
            protocol_version=2,
        )

    def _default_mqtt_info(self) -> MqttConnectionInfo:
        host = self._settings.mqtt_host or ""
        return MqttConnectionInfo(
            connected=self._transport.connected,
            broker_endpoint=f"{host}:{self._settings.mqtt_port}",
            reconnect_count=0,
        )


def _available_memory_mb() -> int:
    """读取当前可用物理内存，检测失败时返回 0。"""
    try:
        if os.name == "nt":
            class MemoryStatus(Structure):
                _fields_ = [
                    ("length", c_ulong),
                    ("memory_load", c_ulong),
                    ("total_physical", c_ulonglong),
                    ("available_physical", c_ulonglong),
                    ("total_page_file", c_ulonglong),
                    ("available_page_file", c_ulonglong),
                    ("total_virtual", c_ulonglong),
                    ("available_virtual", c_ulonglong),
                    ("available_extended_virtual", c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(byref(status)):
                return int(status.available_physical // (1024 * 1024))
        else:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, AttributeError, ValueError):
        return 0
    return 0
