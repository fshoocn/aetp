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
from collections.abc import Callable

from aetp_protocol.capabilities import NodeCapabilitySnapshot
from aetp_protocol.envelope import Envelope
from aetp_protocol.logs import RunLogBatch
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    DiagnosticsSnapshot,
    NodeHeartbeatPayload,
    NodeRegisterPayload,
    PresencePayload,
    RunAckPayload,
    RunCaseStatusPayload,
    RunLogCompletePayload,
    RunProgressPayload,
    RunResultPayload,
    ScriptVerifyResultPayload,
)
from aetp_protocol.topics import (
    validate_message_type_for_topic,
    validate_message_type_for_v2_topic,
    validate_sender_for_topic,
    validate_sender_for_v2_topic,
)
from aetp_protocol.v2_envelope import parse_v2_message

from common.transport import MqttMessage
from master.application.services.capability_snapshot_service import (
    CapabilitySnapshotProjectionService,
    DiagnosticsSnapshotProjectionService,
)
from master.application.services.event_publisher import EventPublisher
from master.application.services.node_presence_service import (
    NodePresenceError,
    NodePresenceService,
)
from master.application.services.run_projection_service import (
    ProjectionResult,
    RunProjectionService,
)
from master.application.services.script_verification_service import (
    ScriptVerificationResult,
    ScriptVerificationService,
)
from master.application.services.shard_scheduler_service import ShardSchedulerService

logger = logging.getLogger(__name__)


class MasterMessageRouter:
    """校验并路由 Agent 上报事件（纯依赖，可单测）。"""

    def __init__(
        self,
        node_presence: NodePresenceService,
        projection: RunProjectionService,
        event_publisher: EventPublisher,
        verification: ScriptVerificationService,
        scheduler: ShardSchedulerService | None = None,
        uow_factory=None,
        capability_snapshot: CapabilitySnapshotProjectionService | None = None,
        diagnostics_snapshot: DiagnosticsSnapshotProjectionService | None = None,
    ) -> None:
        self._node_presence = node_presence
        self._projection = projection
        self._event_publisher = event_publisher
        self._verification = verification
        self._scheduler = scheduler
        self._uow_factory = uow_factory
        self._capability_snapshot = capability_snapshot
        self._diagnostics_snapshot = diagnostics_snapshot
        # 路由表：MessageType → (Payload 类型, 处理函数)
        # Node 事件返回 OutboxMessage；Run 事件返回 ProjectionResult
        self._handlers: dict[
            MessageType,
            tuple[type, Callable[..., object]],
        ] = {
            MessageType.NODE_REGISTER: (
                NodeRegisterPayload,
                lambda e, p: self._node_presence.handle_register(envelope=e, payload=p),
            ),
            MessageType.NODE_HEARTBEAT: (
                NodeHeartbeatPayload,
                lambda e, p: self._node_presence.handle_heartbeat(envelope=e, payload=p),
            ),
            MessageType.PRESENCE: (
                PresencePayload,
                lambda e, p: self._node_presence.handle_presence(envelope=e, payload=p),
            ),
            MessageType.RUN_ACK: (
                RunAckPayload,
                lambda e, p: self._projection.handle_ack(e.sender.id, p),
            ),
            MessageType.RUN_PROGRESS: (
                RunProgressPayload,
                lambda e, p: self._projection.handle_progress(e.sender.id, p),
            ),
            MessageType.RUN_CASE_STATUS: (
                RunCaseStatusPayload,
                lambda e, p: self._projection.handle_case_status(e.sender.id, p),
            ),
            MessageType.RUN_LOG: (
                RunLogBatch,
                lambda e, p: self._projection.handle_log(e.sender.id, p),
            ),
            MessageType.RUN_RESULT: (
                RunResultPayload,
                lambda e, p: self._projection.handle_result(e.sender.id, p),
            ),
            MessageType.RUN_LOG_COMPLETE: (
                RunLogCompletePayload,
                lambda e, p: self._projection.handle_log_complete(e.sender.id, p),
            ),
            MessageType.SCRIPT_VERIFY_RESULT: (
                ScriptVerifyResultPayload,
                lambda e, p: self._verification.handle_result(e.sender.id, p),
            ),
        }

    async def handle(self, message: MqttMessage) -> bool:
        """处理一条入站消息；成功返回 True。"""
        if message.topic.startswith("aetp/v2/"):
            return await self._handle_v2(message)
        try:
            envelope = Envelope.model_validate(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_topic(message.topic, envelope.sender)
            validate_message_type_for_topic(message.topic, MessageType(envelope.message_type))
        except Exception:  # noqa: BLE001 - 协议错误静默忽略
            logger.warning("入站消息校验失败: topic=%s", message.topic)
            return False

        msg_type = MessageType(envelope.message_type)
        handler_entry = self._handlers.get(msg_type)
        if handler_entry is None:
            logger.debug("未处理的入站消息类型: %s", envelope.message_type)
            return False

        payload_cls, handler = handler_entry
        try:
            payload = payload_cls.model_validate(envelope.payload)
            result = handler(envelope, payload)
            if msg_type is MessageType.NODE_REGISTER and self._scheduler is not None:
                try:
                    self._scheduler.reschedule_pending_runs(node_id=envelope.sender.id)
                except Exception:
                    logger.exception(
                        "节点上线后的补偿调度失败（不阻塞注册）: node=%s",
                        envelope.sender.id,
                    )
            # Run 事件需要 publish SSE（结果类型为 ProjectionResult）
            if isinstance(result, ProjectionResult) and result.handled:
                await self._publish(result)
                # P8.6：广播业务异常事件（通知失败不回滚业务状态）
                for event in result.anomaly_events or []:
                    try:
                        await self._event_publisher.broadcast(event)
                    except Exception:
                        logger.exception(
                            "业务异常事件广播失败（不阻塞主流程）: event=%s",
                            event.event_type,
                        )
                # ACK 被拒绝：触发 failover 重派
                if result.retry_dispatch and self._scheduler is not None:
                    try:
                        self._scheduler.schedule_run(result.run_id)
                    except Exception:
                        logger.exception(
                            "ACK 拒绝后重派失败（不阻塞主流程）: run_id=%s",
                            result.run_id,
                        )
                    else:
                        # 重派后若 Run 已收敛为 FAILED（派发耗尽），发布失败事件
                        await self._publish_run_failed_if_terminal(result.run_id)
            elif isinstance(result, ScriptVerificationResult):
                await self._event_publisher.broadcast(result.event)
            return True
        except NodePresenceError as exc:
            logger.warning("节点在线投影拒绝: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001 - 单条处理失败不影响循环
            logger.warning(
                "事件处理失败: type=%s node=%s error=%s",
                envelope.message_type,
                envelope.sender.id,
                exc,
            )
            return False

    async def _handle_v2(self, message: MqttMessage) -> bool:
        """处理 M2 能力/诊断事件，拒绝未实现的 V2 事件。"""
        try:
            envelope, payload = parse_v2_message(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_v2_topic(message.topic, envelope.sender)
            validate_message_type_for_v2_topic(
                message.topic,
                MessageType(envelope.message_type),
            )
            if not isinstance(payload, (NodeCapabilitySnapshot, DiagnosticsSnapshot)):
                return False
            if payload.node_id != envelope.sender.id:
                raise ValueError("V2 payload node_id 与 sender.id 不一致")
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
        except Exception as exc:  # noqa: BLE001 - 单条 V2 事件不能中断消费循环
            logger.warning("V2 入站消息处理失败: topic=%s error=%s", message.topic, exc)
            return False

    async def _publish(self, result: ProjectionResult) -> None:
        """把投影结果转为 SSE 领域事件广播（项目范围）。"""
        if not result.handled:
            return
        data = result.payload or {}
        data["project_id"] = result.project_id
        data["run_id"] = result.run_id
        await self._event_publisher.publish(
            result.event_type,
            data,
            project_id=result.project_id,
            aggregate_id=result.run_id,
        )

    async def _publish_run_failed_if_terminal(self, run_id: str) -> None:
        """重派后若 Run 已收敛为 FAILED（派发耗尽），发布 run.failed 事件。

        failover 重派时调度器可能把 Run 收敛到 FAILED（§8.4），但该路径
        没有 result 上报，需在此补发失败事件供前端时间线展示。
        """
        if self._uow_factory is None:
            return
        from master.domain.enums import RunStatus

        try:
            with self._uow_factory() as uow:
                run = uow.task_runs.get_by_run_id(run_id)
                if run is None or run.status is not RunStatus.FAILED:
                    return
                project_id = run.project_id
            await self._event_publisher.publish(
                "run.failed",
                {"run_id": run_id, "task_id": run.task_id, "reason": "派发耗尽（无可用节点或 failover 不允许）"},
                project_id=project_id,
                aggregate_id=run_id,
            )
        except Exception:
            logger.exception("run.failed 事件发布失败（不阻断主流程）: run=%s", run_id)
