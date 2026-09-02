"""M5 Master AgentLogBatch 接收、幂等和回执测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, stable_id
from aetp_protocol.logs import AgentLogBatch, LogCode, LogContext, LogEvent, LogLevel
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import AgentLogReceived
from aetp_protocol.topics import v2_event_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender, parse_v2_message

from common.transport import MqttMessage
from tests.test_m3_plan_lease import NODE_ID, SESSION_ID, _seed_node


def _message(
    batch: AgentLogBatch,
    *,
    session_id: SessionId,
    message_id: str,
) -> MqttMessage:
    envelope = V2Envelope(
        message_id=MessageId(message_id),
        correlation_id=None,
        sent_at=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
        sender=V2Sender(kind="agent", id=batch.node_id, session_id=session_id),
        message_type=MessageType.AGENT_LOG_BATCH.value,
        trace_id=TraceId("m5-agent-log-trace-0001"),
        payload=batch.model_dump(mode="json"),
    )
    return MqttMessage(
        topic=v2_event_topic(batch.node_id.root, "agent.log.batch"),
        payload=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
    )


def _batch(*, session_id: SessionId = SESSION_ID) -> AgentLogBatch:
    return AgentLogBatch(
        node_id=NODE_ID,
        session_id=session_id,
        first_sequence=1,
        events=(
            LogEvent(
                event_id=BusinessId("01J00000000000000000000091"),
                source="agent",
                source_id=NODE_ID.root,
                sequence=1,
                occurred_at=datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
                level=LogLevel.INFO,
                component="agent.runtime",
                event_code=LogCode("agent.runtime.started"),
                message_template="Agent started",
                message="Agent started",
                context=LogContext(node_id=NODE_ID),
                detail={"healthy": True},
            ),
        ),
    )


def test_master_ingests_agent_log_and_acks_idempotently(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    router = container.message_router()
    message = _message(_batch(), session_id=SESSION_ID, message_id="m5-log-message-0001")

    assert asyncio.run(router.handle(message)) is True
    assert asyncio.run(router.handle(message)) is True

    with container.uow_factory()() as uow:
        records = uow.agent_logs.list(NODE_ID)
        assert len(records) == 1
        assert records[0].event.event_code.root == "agent.runtime.started"
        outbox_id = stable_id("agent-log-received:01J00000000000000000000000:session-00000001:1:1").root
        outbox = uow.outbox_messages.get_by_outbox_id(outbox_id)
        assert outbox is not None
        envelope, payload = parse_v2_message(outbox.payload)
        assert envelope.message_type == MessageType.AGENT_LOG_RECEIVED.value
        assert envelope.correlation_id == MessageId("m5-log-message-0001")
        assert isinstance(payload, AgentLogReceived)
        assert payload.accepted is True


def test_master_rejects_agent_log_from_old_session_with_structured_ack(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    router = container.message_router()
    old_session = SessionId("session-00000002")
    message = _message(
        _batch(session_id=old_session),
        session_id=old_session,
        message_id="m5-log-message-0002",
    )

    assert asyncio.run(router.handle(message)) is True

    with container.uow_factory()() as uow:
        assert uow.agent_logs.list(NODE_ID) == []
        outbox_id = stable_id("agent-log-received:01J00000000000000000000000:session-00000002:1:1").root
        outbox = uow.outbox_messages.get_by_outbox_id(outbox_id)
        assert outbox is not None
        _envelope, payload = parse_v2_message(outbox.payload)
        assert isinstance(payload, AgentLogReceived)
        assert payload.accepted is False
        assert payload.code is not None and payload.code.root == "STALE_SESSION"


def test_master_rejects_agent_log_batch_session_mismatch(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    batch = _batch(session_id=SessionId("session-00000002"))
    message = _message(
        batch,
        session_id=SESSION_ID,
        message_id="m5-log-message-0003",
    )

    assert asyncio.run(container.message_router().handle(message)) is True

    with container.uow_factory()() as uow:
        assert uow.agent_logs.list(NODE_ID) == []
        outbox_id = stable_id(
            "agent-log-received:01J00000000000000000000000:session-00000001:1:1"
        ).root
        _envelope, payload = parse_v2_message(
            uow.outbox_messages.get_by_outbox_id(outbox_id).payload
        )
        assert isinstance(payload, AgentLogReceived)
        assert payload.accepted is False
        assert payload.code is not None and payload.code.root == "STALE_SESSION"


def test_master_rejects_agent_log_event_from_another_node(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    event = _batch().events[0].model_copy(update={"source_id": "other-agent"})
    message = _message(
        _batch().model_copy(update={"events": (event,)}),
        session_id=SESSION_ID,
        message_id="m5-log-message-0004",
    )

    assert asyncio.run(container.message_router().handle(message)) is True

    with container.uow_factory()() as uow:
        assert uow.agent_logs.list(NODE_ID) == []
        outbox_id = stable_id(
            "agent-log-received:01J00000000000000000000000:session-00000001:1:1"
        ).root
        _envelope, payload = parse_v2_message(
            uow.outbox_messages.get_by_outbox_id(outbox_id).payload
        )
        assert isinstance(payload, AgentLogReceived)
        assert payload.accepted is False
        assert payload.code is not None and payload.code.root == "AGENT_IDENTITY_MISMATCH"
