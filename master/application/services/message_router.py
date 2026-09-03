"""Master 入站 Agent 事件路由（P6.4，§9.6 阶段 E）。

接收 Agent 上报的 events 主题消息，严格校验 Envelope 与 sender 身份后，
路由到对应的投影/在线服务：

- node.register / node.heartbeat / presence（LWT）→ NodePresenceService；
- run.ack / run.progress / run.log / run.case-status / run.result →
  RunProjectionService。

失败 fail-open：单条非法/未识别消息只记录，不中断 MQTT 消费循环。
"""

from __future__ import annotations

import json
import logging

from aetp_protocol.capabilities import NodeCapabilitySnapshot
from aetp_protocol.envelope import parse_message
from aetp_protocol.logs import AgentLogBatch
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    CaseStatusEvent,
    DiagnosticsSnapshot,
    ExecutionAck,
    ExecutionFinished,
    ExecutionLogBatch,
    ExecutionProgress,
    ExecutionReconcile,
    Heartbeat,
    LeaseRenewRequest,
    LogComplete,
    LogLevelUpdateResult,
    MaintenanceDrainResult,
    MaintenanceRestartResult,
    MaintenanceStatus,
    NodeRegister,
    Presence,
)
from aetp_protocol.plugins import PluginSyncResult
from aetp_protocol.topics import (
    validate_message_type_for_topic,
    validate_sender_for_topic,
)

from common.transport import MqttMessage
from master.application.services.agent_log_service import AgentLogService
from master.application.services.agent_maintenance_service import AgentMaintenanceService
from master.application.services.capability_snapshot_service import (
    CapabilitySnapshotProjectionService,
    DiagnosticsSnapshotProjectionService,
)
from master.application.services.event_publisher import EventPublisher
from master.application.services.execution_service import ExecutionService
from master.application.services.node_presence_service import NodePresenceService
from master.application.services.plugin_sync_service import PluginSyncService
from master.application.services.scheduler_service import SchedulerService

logger = logging.getLogger(__name__)


class MasterMessageRouter:
    """校验并路由 Agent 上报事件（纯依赖，可单测）。"""

    def __init__(
        self,
        node_presence: NodePresenceService,
        event_publisher: EventPublisher,
        scheduler: SchedulerService,
        uow_factory,
        capability_snapshot: CapabilitySnapshotProjectionService,
        diagnostics_snapshot: DiagnosticsSnapshotProjectionService,
        plugin_sync: PluginSyncService,
        execution: ExecutionService,
        agent_logs: AgentLogService,
        maintenance: AgentMaintenanceService,
    ) -> None:
        self._node_presence = node_presence
        self._event_publisher = event_publisher
        self._scheduler = scheduler
        self._uow_factory = uow_factory
        self._capability_snapshot = capability_snapshot
        self._diagnostics_snapshot = diagnostics_snapshot
        self._plugin_sync = plugin_sync
        self._execution = execution
        self._agent_logs = agent_logs
        self._maintenance = maintenance

    async def handle(self, message: MqttMessage) -> bool:
        """处理一条当前协议入站消息；成功返回 True。"""
        return await self._handle(message)

    async def _handle(self, message: MqttMessage) -> bool:
        """处理注册、能力和诊断事件，拒绝未实现的事件。"""
        try:
            envelope, payload = parse_message(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_topic(message.topic, envelope.sender)
            validate_message_type_for_topic(
                message.topic,
                MessageType(envelope.message_type),
            )
            if isinstance(payload, NodeRegister):
                if payload.session_id != envelope.sender.session_id:
                    raise ValueError(" 注册 payload session_id 与 sender 不一致")
                self._node_presence.handle_register(
                    envelope=envelope,
                    payload=payload,
                )
                if self._capability_snapshot is not None:
                    self._capability_snapshot.accept(
                        payload.capability_snapshot,
                        sender_session_id=envelope.sender.session_id,
                    )
                if self._maintenance is not None:
                    self._maintenance.on_session_registered(
                        envelope.sender.id,
                        envelope.sender.session_id,
                    )
                try:
                    self._scheduler.reschedule_pending_runs(node_id=envelope.sender.id.root)
                except Exception:
                    logger.exception(
                        "节点上线后的补偿调度失败（不阻塞注册）: node=%s",
                        envelope.sender.id.root,
                    )
                return True
            if isinstance(payload, Heartbeat):
                self._node_presence.handle_heartbeat(
                    envelope=envelope,
                    payload=payload,
                )
                return True
            if isinstance(payload, Presence):
                self._node_presence.handle_presence(
                    envelope=envelope,
                    payload=payload,
                )
                return True
            if isinstance(payload, ExecutionAck):
                if envelope.correlation_id is None:
                    return False
                return self._execution.handle_execution_ack(
                    payload,
                    sender_node_id=envelope.sender.id,
                    sender_session_id=envelope.sender.session_id,
                )
            if isinstance(payload, ExecutionFinished):
                if envelope.correlation_id is None:
                    return False
                return self._execution.handle_execution_finished(
                    payload,
                    sender_node_id=envelope.sender.id,
                    sender_session_id=envelope.sender.session_id,
                )
            if isinstance(payload, ExecutionProgress):
                return self._execution.handle_execution_progress(
                    payload,
                    sender_node_id=envelope.sender.id,
                    sender_session_id=envelope.sender.session_id,
                )
            if isinstance(payload, ExecutionLogBatch):
                return self._execution.handle_execution_log(
                    payload,
                    sender_node_id=envelope.sender.id,
                    sender_session_id=envelope.sender.session_id,
                )
            if isinstance(payload, CaseStatusEvent):
                return self._execution.handle_execution_case_status(
                    payload,
                    sender_node_id=envelope.sender.id,
                    sender_session_id=envelope.sender.session_id,
                )
            if isinstance(payload, LogComplete):
                return self._execution.handle_execution_log_complete(
                    payload,
                    sender_node_id=envelope.sender.id,
                    sender_session_id=envelope.sender.session_id,
                )
            if isinstance(payload, ExecutionReconcile):
                return self._execution.handle_execution_reconcile(
                    payload,
                    message_id=envelope.message_id,
                    sender_node_id=envelope.sender.id,
                    sender_session_id=envelope.sender.session_id,
                )
            if isinstance(payload, AgentLogBatch):
                if self._agent_logs is None:
                    return False
                result = self._agent_logs.ingest(
                    payload,
                    message_id=envelope.message_id,
                    sender_session_id=envelope.sender.session_id,
                    sender_node_id=envelope.sender.id,
                )
                for record in result.records:
                    await self._event_publisher.broadcast_agent_log(record)
                return True
            if isinstance(payload, LeaseRenewRequest):
                return self._execution.handle_lease_renew(
                    payload,
                    message_id=envelope.message_id,
                    sender_node_id=envelope.sender.id,
                    sender_session_id=envelope.sender.session_id,
                )
            if (
                isinstance(
                    payload,
                    (
                        NodeCapabilitySnapshot,
                        DiagnosticsSnapshot,
                        PluginSyncResult,
                        MaintenanceStatus,
                        LogLevelUpdateResult,
                        MaintenanceDrainResult,
                        MaintenanceRestartResult,
                        ExecutionReconcile,
                    ),
                )
                and payload.node_id != envelope.sender.id
            ):
                raise ValueError(" payload node_id 与 sender.id 不一致")
            if isinstance(payload, PluginSyncResult):
                if self._plugin_sync is None or envelope.correlation_id is None:
                    return False
                self._plugin_sync.record_result(
                    payload,
                    sender_session_id=envelope.sender.session_id,
                )
                return True
            if isinstance(payload, MaintenanceStatus):
                if self._plugin_sync is None:
                    return False
                self._plugin_sync.record_maintenance_status(
                    payload,
                    sender_session_id=envelope.sender.session_id,
                )
                return True
            if isinstance(payload, LogLevelUpdateResult):
                if self._maintenance is None:
                    return False
                self._maintenance.handle_log_level_result(
                    payload,
                    sender_session_id=envelope.sender.session_id,
                )
                return True
            if isinstance(payload, MaintenanceDrainResult):
                if self._maintenance is None:
                    return False
                self._maintenance.handle_drain_result(
                    payload,
                    sender_session_id=envelope.sender.session_id,
                )
                return True
            if isinstance(payload, MaintenanceRestartResult):
                if self._maintenance is None:
                    return False
                self._maintenance.handle_restart_result(
                    payload,
                    sender_session_id=envelope.sender.session_id,
                )
                return True
            if not isinstance(payload, (NodeCapabilitySnapshot, DiagnosticsSnapshot)):
                return False
            if isinstance(payload, NodeCapabilitySnapshot):
                if self._capability_snapshot is None:
                    return False
                self._capability_snapshot.accept(
                    payload,
                    sender_session_id=envelope.sender.session_id,
                )
                return True
            if self._diagnostics_snapshot is None:
                return False
            self._diagnostics_snapshot.accept(
                payload,
                sender_session_id=envelope.sender.session_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 单条  事件不能中断消费循环
            logger.warning(" 入站消息处理失败: topic=%s error=%s", message.topic, exc)
            return False
