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
from aetp_protocol.ids import BusinessId, MessageId, SemVer, SessionId, TraceId, Version, new_id
from aetp_protocol.logs import LogEvent
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    ActiveAttemptInfo,
    AgentSystemInfo,
    DiagnosticsRequest,
    DiagnosticsSnapshot,
    MqttConnectionInfo,
)
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

    def build_snapshot(self, session_id: SessionId) -> NodeCapabilitySnapshot:
        """生成当前 session 的下一版能力快照。"""
        service = AgentCapabilitySnapshotService(
            node_id=self._node_id(),
            session_id=session_id,
            registry=self._registry,
            tags=self._tags,
            maintenance_state=AgentMaintenanceState.IDLE,
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
            maintenance_state=AgentMaintenanceState.IDLE,
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
        payload: NodeCapabilitySnapshot | DiagnosticsSnapshot,
        session_id: SessionId,
        correlation_id: MessageId | None = None,
    ) -> None:
        envelope = V2Envelope(
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

    @staticmethod
    def _message_segment(message_type: MessageType) -> str:
        return {
            MessageType.NODE_CAPABILITY_SNAPSHOT: "capability.snapshot",
            MessageType.AGENT_DIAGNOSTICS_SNAPSHOT: "agent.diagnostics.snapshot",
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
