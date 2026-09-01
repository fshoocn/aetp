"""Master V2 远程诊断请求服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aetp_protocol.ids import BusinessId, MessageId, RequestId, SessionId, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import DiagnosticsRequest
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from master.domain.enums import OutboxStatus
from master.domain.models import OutboxMessage
from master.domain.repositories import UnitOfWork


@dataclass(frozen=True)
class DiagnosticsRequestOperation:
    """已写入 Outbox 的远程诊断请求。"""

    operation_id: BusinessId
    request: DiagnosticsRequest
    outbox: OutboxMessage


class AgentOfflineForDiagnostics(ValueError):
    """节点没有当前可用 session，无法下发诊断请求。"""


class DiagnosticsRequestService:
    """创建并可靠下发 Agent 诊断请求。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def request(
        self,
        node_id: BusinessId,
        *,
        include_log_tail: bool = True,
        log_tail_count: int = 200,
    ) -> DiagnosticsRequestOperation:
        request_id = RequestId(new_id())
        diagnostics_request = DiagnosticsRequest(
            request_id=request_id,
            node_id=node_id,
            include_log_tail=include_log_tail,
            log_tail_count=log_tail_count,
        )
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(node_id.root)
            if node is None or node.id is None:
                raise KeyError(f"节点不存在: {node_id.root}")
            session = uow.node_sessions.get_current(node.id)
            if session is None or not node.online:
                raise AgentOfflineForDiagnostics(f"节点当前离线: {node_id.root}")
            envelope = V2Envelope(
                message_id=MessageId(new_id()),
                sent_at=datetime.now(UTC),
                sender=V2Sender(
                    kind="master",
                    id=stable_id("aetp-master"),
                    session_id=SessionId(stable_id("aetp-master-session").root),
                ),
                message_type=MessageType.AGENT_DIAGNOSTICS_REQUEST.value,
                trace_id=TraceId(new_id()),
                payload=diagnostics_request.model_dump(mode="json"),
            )
            outbox = uow.outbox_messages.enqueue(
                OutboxMessage(
                    outbox_id=f"diagnostics:{request_id.root}",
                    aggregate_type="node",
                    aggregate_id=node_id.root,
                    topic=v2_command_topic(node_id.root, "agent.diagnostics.request"),
                    payload=envelope.model_dump(mode="json"),
                    qos=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=None,
                )
            )
        return DiagnosticsRequestOperation(
            operation_id=BusinessId(request_id.root),
            request=diagnostics_request,
            outbox=outbox,
        )
