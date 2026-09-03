"""Agent  能力和诊断消息发布测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

from aetp_protocol.capabilities import NodeCapabilities, NodeCapabilitySnapshot
from aetp_protocol.envelope import Envelope, Sender, parse_message
from aetp_protocol.ids import BusinessId, MessageId, RequestId, SemVer, SessionId, TraceId, new_id, stable_id
from aetp_protocol.logs import LogCode, LogEvent, LogLevel
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import DiagnosticsRequest, DiagnosticsSnapshot, NodeRegister, NodeRegisterAck
from aetp_protocol.topics import command_topic, event_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.config import AgentSettings
from agent.plugins.registry import PluginRegistry
from common.transport import MqttMessage, Transport

NODE_ID = BusinessId("01J00000000000000000000000")
MASTER_ID = stable_id("aetp-master")
SESSION_ID = SessionId("session-00000001")


class FakeTransport:
    connected = True

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int]] = []

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.published.append((topic, payload, qos))


def _publisher(tmp_path) -> tuple[CapabilityPublisher, FakeTransport]:
    transport = FakeTransport()
    settings = AgentSettings(
        node_id=NODE_ID.root,
        name="Bench 01",
        mqtt_host="broker.test",
        mqtt_port=8883,
        mqtt_client_id="aetp-agent-bench-01",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
    )
    publisher = CapabilityPublisher(
        cast(Transport, transport),
        settings,
        PluginRegistry(),
        capability_scanner=lambda: NodeCapabilities(),
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    return publisher, transport


def test_publisher_emits_typed_capability_snapshot(tmp_path) -> None:
    publisher, transport = _publisher(tmp_path)

    snapshot = asyncio.run(publisher.publish_snapshot(SESSION_ID))

    assert snapshot.revision == 1
    assert transport.published[0][0] == event_topic(NODE_ID.root, "capability.snapshot")
    envelope, payload = parse_message(json.loads(transport.published[0][1]))
    assert envelope.message_type == MessageType.NODE_CAPABILITY_SNAPSHOT.value
    assert isinstance(payload, NodeCapabilitySnapshot)
    assert payload.node_id == NODE_ID
    assert payload.session_id == SESSION_ID
    assert payload.revision == 1


def test_publisher_enqueues_register_and_accepts_correlated_ack(tmp_path) -> None:
    publisher, transport = _publisher(tmp_path)
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")

    message_id = publisher.enqueue_register(ledger, SESSION_ID)
    entries = ledger.claim_due_outbox(10, datetime.now(UTC).replace(tzinfo=None))

    assert len(entries) == 1
    register_envelope, register_payload = parse_message(entries[0].payload)
    assert register_envelope.message_id.root == message_id
    assert isinstance(register_payload, NodeRegister)
    assert register_payload.node_id == NODE_ID
    assert register_payload.session_id == SESSION_ID
    assert entries[0].topic == event_topic(NODE_ID.root, "register")

    ack = Envelope(
        message_id=MessageId(new_id()),
        correlation_id=register_envelope.message_id,
        sent_at=datetime.now(UTC),
        sender=Sender(
            kind="master",
            id=stable_id("aetp-master"),
            session_id=SessionId("master-session-01"),
        ),
        message_type=MessageType.NODE_REGISTER_ACK.value,
        trace_id=TraceId(new_id()),
        payload=NodeRegisterAck(
            node_id=NODE_ID,
            session_id=SESSION_ID,
            accepted=True,
        ).model_dump(mode="json"),
    )

    assert publisher.handle_register_ack(
        MqttMessage(
            topic=publisher.register_ack_topic(),
            payload=ack.model_dump_json().encode("utf-8"),
        ),
        SESSION_ID,
    ) is True
    assert publisher.registered is True
    assert transport.published == []


def test_diagnostics_snapshot_contains_typed_system_and_log_tail(tmp_path) -> None:
    log_event = LogEvent(
        event_id=BusinessId("01J00000000000000000000002"),
        source="agent",
        source_id=NODE_ID.root,
        sequence=1,
        occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
        level=LogLevel.INFO,
        component="capability",
        event_code=LogCode("agent.capability.snapshot"),
        message_template="snapshot generated",
        message="snapshot generated",
    )
    publisher, transport = _publisher(tmp_path)
    publisher._log_tail = lambda _count: (log_event,)
    request = DiagnosticsRequest(
        request_id=RequestId("request-00000001"),
        node_id=NODE_ID,
        include_log_tail=True,
        log_tail_count=20,
    )

    snapshot = asyncio.run(publisher.publish_diagnostics(request, SESSION_ID))

    assert snapshot.node_id == NODE_ID
    assert snapshot.system.agent_version == SemVer("2.0.0")
    assert snapshot.system.protocol_version == 2
    assert snapshot.log_tail == (log_event,)
    assert transport.published[0][0] == event_topic(NODE_ID.root, "agent.diagnostics.snapshot")
    envelope, payload = parse_message(json.loads(transport.published[0][1]))
    assert envelope.message_type == MessageType.AGENT_DIAGNOSTICS_SNAPSHOT.value
    assert isinstance(payload, DiagnosticsSnapshot)
    assert payload.request_id == request.request_id


def test_diagnostics_request_handler_rejects_non_master_and_handles_request(tmp_path) -> None:
    publisher, transport = _publisher(tmp_path)
    request = DiagnosticsRequest(
        request_id=RequestId("request-00000002"),
        node_id=NODE_ID,
        include_log_tail=False,
    )
    envelope = Envelope(
        message_id=MessageId(new_id()),
        sent_at=datetime.now(UTC),
        sender=Sender(kind="master", id=MASTER_ID, session_id=SessionId("master-session-01")),
        message_type=MessageType.AGENT_DIAGNOSTICS_REQUEST.value,
        trace_id=TraceId(new_id()),
        payload=request.model_dump(mode="json"),
    )

    handled = asyncio.run(
        publisher.handle_diagnostics_request(
            MqttMessage(
                topic=command_topic(NODE_ID.root, "agent.diagnostics.request"),
                payload=envelope.model_dump_json().encode("utf-8"),
            ),
            SESSION_ID,
        )
    )

    assert handled is True
    assert len(transport.published) == 1
