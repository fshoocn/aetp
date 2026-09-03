"""Master Agent 结构化日志接收、幂等和回执服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, new_id, stable_id
from aetp_protocol.logs import AgentLogBatch
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import AgentLogReceived
from aetp_protocol.topics import command_topic

from master.domain.enums import OutboxStatus
from master.domain.models import AgentLogEventRecord, OutboxMessage
from master.domain.repositories import UnitOfWork


@dataclass(frozen=True)
class AgentLogIngestResult:
    """一次 Agent 日志批次接收结果。"""

    receipt: AgentLogReceived
    records: tuple[AgentLogEventRecord, ...] = ()


class AgentLogService:
    """接收 Agent 结构化日志并生成可靠业务 ACK。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        master_id: str = "aetp-master",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._master_id = master_id
        self._now = now or (lambda: datetime.now(UTC))

    def ingest(
        self,
        batch: AgentLogBatch,
        *,
        message_id: MessageId,
        sender_session_id: SessionId,
        sender_node_id: BusinessId | None = None,
    ) -> AgentLogIngestResult:
        """校验 session、写入新日志并在同一事务中生成 ACK。"""
        last_sequence = batch.events[-1].sequence
        receipt_node_id = sender_node_id or batch.node_id
        with self._uow_factory() as uow:
            if sender_node_id is not None and batch.node_id != sender_node_id:
                receipt = AgentLogReceived(
                    node_id=receipt_node_id,
                    session_id=sender_session_id,
                    first_sequence=batch.first_sequence,
                    last_sequence=last_sequence,
                    accepted=False,
                    code=ErrorCode("AGENT_IDENTITY_MISMATCH"),
                    message="日志批次 node_id 与 Envelope sender.id 不一致",
                )
                self._enqueue_receipt(uow, message_id, receipt)
                return AgentLogIngestResult(receipt=receipt)
            if batch.session_id != sender_session_id:
                receipt = AgentLogReceived(
                    node_id=receipt_node_id,
                    session_id=sender_session_id,
                    first_sequence=batch.first_sequence,
                    last_sequence=last_sequence,
                    accepted=False,
                    code=ErrorCode("STALE_SESSION"),
                    message="日志批次 session_id 与 Envelope sender.session_id 不一致",
                )
                self._enqueue_receipt(uow, message_id, receipt)
                return AgentLogIngestResult(receipt=receipt)
            node = uow.nodes.get_by_id(batch.node_id.root)
            current = (
                uow.node_sessions.get_current(node.id)
                if node is not None and node.id is not None
                else None
            )
            if node is None or current is None or current.session_id != sender_session_id.root:
                receipt = AgentLogReceived(
                    node_id=batch.node_id,
                    session_id=sender_session_id,
                    first_sequence=batch.first_sequence,
                    last_sequence=last_sequence,
                    accepted=False,
                    code=ErrorCode("STALE_SESSION"),
                    message="日志批次来自非当前 Agent session",
                )
                self._enqueue_receipt(uow, message_id, receipt)
                return AgentLogIngestResult(receipt=receipt)

            for event in batch.events:
                if (
                    event.source != "agent"
                    or event.source_id != batch.node_id.root
                    or (
                        event.context.node_id is not None
                        and event.context.node_id != batch.node_id
                    )
                ):
                    receipt = AgentLogReceived(
                        node_id=batch.node_id,
                        session_id=sender_session_id,
                        first_sequence=batch.first_sequence,
                        last_sequence=last_sequence,
                        accepted=False,
                        code=ErrorCode("AGENT_IDENTITY_MISMATCH"),
                        message="日志事件 context.node_id 与批次节点不一致",
                    )
                    self._enqueue_receipt(uow, message_id, receipt)
                    return AgentLogIngestResult(receipt=receipt)

            existing = uow.agent_logs.existing_sequences(
                batch.node_id,
                sender_session_id.root,
                [event.sequence for event in batch.events],
            )
            new_records = tuple(
                AgentLogEventRecord(
                    id=None,
                    node_id=batch.node_id,
                    session_id=sender_session_id,
                    sequence=event.sequence,
                    event=event,
                    batch_first_sequence=batch.first_sequence,
                    received_at=self._now(),
                    created_at=None,
                )
                for event in batch.events
                if event.sequence not in existing
            )
            if new_records:
                uow.agent_logs.add_many(list(new_records))
            receipt = AgentLogReceived(
                node_id=batch.node_id,
                session_id=sender_session_id,
                first_sequence=batch.first_sequence,
                last_sequence=last_sequence,
            )
            self._enqueue_receipt(uow, message_id, receipt)
            return AgentLogIngestResult(receipt=receipt, records=new_records)

    def list_logs(
        self,
        node_id: BusinessId,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
        level: str | None = None,
        component: str | None = None,
        event_code: str | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        plugin_id: str | None = None,
        keyword: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
    ) -> list[AgentLogEventRecord]:
        """按节点和结构化字段查询已接收日志。"""
        if after_sequence < 0:
            raise ValueError("after_sequence 不能小于 0")
        if occurred_after is not None and occurred_before is not None and occurred_after > occurred_before:
            raise ValueError("occurred_after 不能晚于 occurred_before")
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(node_id.root)
            if node is None:
                raise KeyError(f"节点不存在: {node_id.root}")
            return uow.agent_logs.list(
                node_id,
                session_id=session_id,
                after_sequence=after_sequence,
                limit=limit,
                level=level,
                component=component,
                event_code=event_code,
                run_id=run_id,
                attempt_id=attempt_id,
                plugin_id=plugin_id,
                keyword=keyword,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
            )

    def _enqueue_receipt(
        self,
        uow: UnitOfWork,
        message_id: MessageId,
        receipt: AgentLogReceived,
    ) -> None:
        outbox_id = stable_id(
            f"agent-log-received:{receipt.node_id.root}:{receipt.session_id.root}:"
            f"{receipt.first_sequence}:{receipt.last_sequence}"
        ).root
        if uow.outbox_messages.get_by_outbox_id(outbox_id) is not None:
            return
        envelope = Envelope(
            message_id=MessageId(new_id()),
            correlation_id=message_id,
            sent_at=self._now(),
            sender=Sender(
                kind=SenderKind.MASTER,
                id=stable_id(self._master_id),
                session_id=SessionId(stable_id(f"{self._master_id}:session").root),
            ),
            message_type=MessageType.AGENT_LOG_RECEIVED.value,
            trace_id=TraceId(new_id()),
            payload=receipt.model_dump(mode="json"),
        )
        uow.outbox_messages.enqueue(
            OutboxMessage(
                outbox_id=outbox_id,
                aggregate_type="agent_log",
                aggregate_id=receipt.node_id.root,
                topic=command_topic(receipt.node_id.root, "agent.log.received"),
                payload=envelope.model_dump(mode="json"),
                qos=1,
                status=OutboxStatus.PENDING,
                attempts=0,
                next_attempt_at=None,
            )
        )


__all__ = ["AgentLogIngestResult", "AgentLogService"]
