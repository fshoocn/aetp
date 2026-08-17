"""P5.3：AgentRuntime 生命周期测试。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.topics import command_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.runtime import AgentRuntime
from agent.application.services.registration_service import RegistrationService
from agent.config import AgentSettings
from common.transport import MqttMessage


class RuntimeTransport:
    """可驱动连接回调与入站消息的假 Transport。"""

    def __init__(self) -> None:
        self.connected = False
        self.message_handler = None
        self.connection_handler = None
        self.subscriptions: list[str] = []
        self.published: list[tuple[str, bytes, int]] = []
        self.session_id = "session-1"

    def on_message(self, handler) -> None:
        self.message_handler = handler

    def on_connection_change(self, handler) -> None:
        self.connection_handler = handler

    async def connect(self) -> None:
        self.connected = True
        await self.connection_handler(True, self.session_id)

    async def disconnect(self) -> None:
        if self.connected:
            self.connected = False
            await self.connection_handler(False, self.session_id)

    async def subscribe(self, topics: list[str]) -> None:
        self.subscriptions = list(topics)

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.published.append((topic, payload, qos))

    async def emit(self, topic: str, payload: bytes) -> None:
        await self.message_handler(MqttMessage(topic=topic, payload=payload))


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
    registration_timeout_s=1,
    heartbeat_interval_s=60,
)


def _ack(correlation_id: str, session_id: str) -> bytes:
    envelope = Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.REGISTER_ACK.value,
        sent_at=datetime.now(timezone.utc),
        sender=Sender(
            kind=SenderKind.MASTER,
            id="aetp-master",
            session_id="master-session",
        ),
        correlation_id=correlation_id,
        trace_id="bench-001",
        payload={
            "node_id": "bench-001",
            "session_id": session_id,
            "accepted": True,
        },
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


@pytest.mark.asyncio
async def test_runtime_connects_registers_then_starts_heartbeat(tmp_path) -> None:
    transport = RuntimeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(
        transport, ledger, _SETTINGS, session_id="old-session"
    )
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    await runtime.start()
    await asyncio.sleep(0.05)

    assert transport.subscriptions == [
        command_topic("bench-001", "register-ack")
    ]
    assert registration.registered is False
    assert registration.pending_register_message_id is not None
    # 连接成功后已自动写入/发布注册消息，但 ACK 前不发布 heartbeat。
    assert any("/events/register" in topic for topic, _, _ in transport.published)
    assert not any("/events/heartbeat" in topic for topic, _, _ in transport.published)

    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(registration.pending_register_message_id, "session-1"),
    )
    await asyncio.sleep(0.05)
    assert registration.registered is True
    assert any("/events/heartbeat" in topic for topic, _, _ in transport.published)

    await runtime.stop()
    assert registration.registered is False


@pytest.mark.asyncio
async def test_runtime_disconnect_invalidates_registration(tmp_path) -> None:
    transport = RuntimeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(transport, ledger, _SETTINGS)
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    await runtime.start()
    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(registration.pending_register_message_id, "session-1"),
    )
    await asyncio.sleep(0.02)
    assert registration.registered is True

    await transport.disconnect()
    assert registration.registered is False
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_reconnect_uses_new_session_and_registers_again(tmp_path) -> None:
    transport = RuntimeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(transport, ledger, _SETTINGS)
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    await runtime.start()
    first_message_id = registration.pending_register_message_id
    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(first_message_id, "session-1"),
    )
    await asyncio.sleep(0.02)
    assert registration.registered is True

    await transport.disconnect()
    transport.session_id = "session-2"
    await transport.connect()
    await asyncio.sleep(0.02)

    assert registration.registered is False
    assert registration.session_id == "session-2"
    assert registration.pending_register_message_id != first_message_id

    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(registration.pending_register_message_id, "session-2"),
    )
    await asyncio.sleep(0.02)
    assert registration.registered is True
    await runtime.stop()
