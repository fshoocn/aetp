"""P5.3：Agent 注册 / register-ack 校验 / 心跳测试。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import NodeHeartbeatPayload, NodeRegisterPayload
from aetp_protocol.topics import command_topic, event_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.registration_service import RegistrationService
from agent.config import AgentSettings
from common.transport import MqttMessage


class FakeTransport:
    """记录发布的消息；connected 可开关。"""

    def __init__(self) -> None:
        self.connected = True
        self.published: list[tuple[str, bytes, int]] = []
        self.handler = None

    def on_message(self, handler) -> None:
        self.handler = handler

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def subscribe(self, topics: list[str]) -> None:
        pass

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.published.append((topic, payload, qos))


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="CAN 台架 01",
    master_id="aetp-master",
    mqtt_host="broker.test",
    mqtt_port=1883,
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
    heartbeat_interval_s=5,
)


def _now() -> datetime:
    return datetime(2026, 8, 17, 8, 0, 0, tzinfo=timezone.utc)


def _ack_envelope(
    session_id: str,
    *,
    correlation_id: str | None = None,
    node_id: str = "bench-001",
    accepted: bool = True,
) -> Envelope:
    """构造 Master 下发的 register-ack Envelope。"""
    return Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.REGISTER_ACK.value,
        sent_at=_now(),
        sender=Sender(
            kind=SenderKind.MASTER, id="aetp-master", session_id="master-session"
        ),
        correlation_id=correlation_id,
        trace_id="bench-001",
        payload={
            "node_id": node_id,
            "session_id": session_id,
            "accepted": accepted,
        },
    )


def _make_service(tmp_path) -> tuple[RegistrationService, FakeTransport, SQLiteLedger]:
    transport = FakeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    service = RegistrationService(
        transport, ledger, _SETTINGS, session_id="sess-1", now=_now
    )
    return service, transport, ledger


def test_register_enqueue_writes_outbox(tmp_path) -> None:
    service, _transport, ledger = _make_service(tmp_path)
    outbox_id = service.enqueue_register()

    due = ledger.claim_due_outbox(
        10, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
    )
    assert len(due) == 1
    assert due[0].outbox_id == outbox_id
    assert due[0].topic == event_topic("bench-001", "register")
    envelope = Envelope.model_validate(due[0].payload)
    assert envelope.message_type == MessageType.NODE_REGISTER.value
    assert envelope.sender.kind is SenderKind.AGENT
    assert envelope.sender.id == "bench-001"
    assert envelope.sender.session_id == "sess-1"
    # 载荷可反序列化为强类型 NodeRegisterPayload
    payload = NodeRegisterPayload.model_validate(envelope.payload)
    assert payload.node_id == "bench-001"
    assert payload.name == "CAN 台架 01"


@pytest.mark.asyncio
async def test_publish_register_sends_event(tmp_path) -> None:
    service, transport, _ledger = _make_service(tmp_path)
    await service.publish_register()

    assert len(transport.published) == 1
    topic, payload_bytes, qos = transport.published[0]
    assert topic == event_topic("bench-001", "register")
    assert qos == 1
    envelope = Envelope.model_validate(json.loads(payload_bytes))
    assert envelope.sender.id == "bench-001"
    assert envelope.sender.session_id == "sess-1"


def test_register_ack_validates_and_marks_registered(tmp_path) -> None:
    service, _transport, _ledger = _make_service(tmp_path)
    assert service.registered is False

    service.enqueue_register()
    ack = _ack_envelope(
        "sess-1", correlation_id=service.pending_register_message_id
    )
    topic = command_topic("bench-001", "register-ack")
    ok = service.handle_register_ack(
        MqttMessage(
            topic=topic,
            payload=json.dumps(ack.model_dump(mode="json")).encode("utf-8"),
        )
    )
    assert ok is True
    assert service.registered is True


def test_register_ack_rejects_wrong_node(tmp_path) -> None:
    service, _transport, _ledger = _make_service(tmp_path)
    service.enqueue_register()
    ack = _ack_envelope(
        "sess-1",
        correlation_id=service.pending_register_message_id,
        node_id="other-node",
    )
    # 主题 node_id 与自身不符
    topic = command_topic("other-node", "register-ack")
    ok = service.handle_register_ack(
        MqttMessage(
            topic=topic,
            payload=json.dumps(ack.model_dump(mode="json")).encode("utf-8"),
        )
    )
    assert ok is False
    assert service.registered is False


def test_register_ack_rejects_wrong_message_type(tmp_path) -> None:
    service, _transport, _ledger = _make_service(tmp_path)
    service.enqueue_register()
    envelope = _ack_envelope(
        "sess-1", correlation_id=service.pending_register_message_id
    )
    envelope.message_type = MessageType.RUN_ASSIGN.value
    topic = command_topic("bench-001", "assign")
    ok = service.handle_register_ack(
        MqttMessage(
            topic=topic,
            payload=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        )
    )
    assert ok is False
    assert service.registered is False


@pytest.mark.asyncio
async def test_publish_heartbeat_sends_qos0(tmp_path) -> None:
    service, transport, _ledger = _make_service(tmp_path)
    service.enqueue_register()
    ack = _ack_envelope(
        "sess-1", correlation_id=service.pending_register_message_id
    )
    assert service.handle_register_ack(
        MqttMessage(
            topic=command_topic("bench-001", "register-ack"),
            payload=json.dumps(ack.model_dump(mode="json")).encode("utf-8"),
        )
    )
    await service.publish_heartbeat()

    assert len(transport.published) == 1
    topic, payload_bytes, qos = transport.published[0]
    assert topic == event_topic("bench-001", "heartbeat")
    assert qos == 0
    envelope = Envelope.model_validate(json.loads(payload_bytes))
    assert envelope.message_type == MessageType.NODE_HEARTBEAT.value
    payload = NodeHeartbeatPayload.model_validate(envelope.payload)
    assert payload.node_id == "bench-001"
    assert payload.status == "online"


def test_heartbeat_load_reflects_ledger_active_runs(tmp_path) -> None:
    """心跳负载来自账本真实活动 Run，不写死。"""
    service, _transport, ledger = _make_service(tmp_path)
    from agent.domain.enums import AgentRunStatus

    # 无活动 Run：负载全 0
    payload = service.build_heartbeat_payload()
    assert payload.load == {"running_shards": 0, "queued_shards": 0}
    assert payload.active_run_ids == []

    # claim 两个 run（queued），其中一个转 running
    ledger.claim_run("run-1", 1)
    ledger.claim_run("run-2", 1)
    run = ledger.get_run("run-1")
    assert run is not None
    run.status = AgentRunStatus.RUNNING
    ledger.update_run(run)

    payload = service.build_heartbeat_payload()
    assert payload.load == {"running_shards": 1, "queued_shards": 1}
    assert sorted(payload.active_run_ids) == ["run-1", "run-2"]

    # run-2 终结后不再计入
    done = ledger.get_run("run-2")
    assert done is not None
    done.status = AgentRunStatus.SUCCEEDED
    ledger.update_run(done)
    payload = service.build_heartbeat_payload()
    assert payload.load == {"running_shards": 1, "queued_shards": 0}
    assert payload.active_run_ids == ["run-1"]
