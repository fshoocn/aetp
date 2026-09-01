"""M2 AgentRuntime V2 快照接入测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from aetp_protocol.capabilities import NodeCapabilities
from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.ids import BusinessId, SessionId
from aetp_protocol.message_types import MessageType
from aetp_protocol.topics import command_topic, v2_command_topic, v2_event_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.runtime import AgentRuntime
from agent.application.services.registration_service import RegistrationService
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.config import AgentSettings
from agent.plugins.v2_registry import AgentV2PluginRegistry
from common.transport import MqttMessage

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")


class RuntimeTransport:
    connected = False

    def __init__(self) -> None:
        self.message_handler = None
        self.connection_handler = None
        self.subscriptions: list[str] = []
        self.published: list[tuple[str, bytes, int]] = []
        self.session_id = SESSION_ID.root

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


def _settings() -> AgentSettings:
    return AgentSettings(
        node_id=NODE_ID.root,
        name="Bench 01",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-01",
        mqtt_use_tls=False,
        registration_timeout_s=1,
        heartbeat_interval_s=60,
    )


def _ack(correlation_id: str | None, session_id: str) -> bytes:
    envelope = Envelope(
        protocol_version=1,
        message_id="ack-message-0001",
        message_type=MessageType.REGISTER_ACK.value,
        sent_at=datetime.now(UTC),
        sender=Sender(kind=SenderKind.MASTER, id="aetp-master", session_id="master-session"),
        correlation_id=correlation_id,
        trace_id="trace-registration",
        payload={
            "node_id": NODE_ID.root,
            "session_id": session_id,
            "accepted": True,
        },
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


async def _run_runtime(tmp_path):
    transport = RuntimeTransport()
    settings = _settings()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(transport, ledger, settings)
    publisher = AgentV2CapabilityPublisher(
        transport,
        settings,
        AgentV2PluginRegistry(tmp_path / "plugins"),
        capability_scanner=lambda: NodeCapabilities(),
    )
    runtime = AgentRuntime(
        settings,
        transport,
        ledger,
        registration,
        v2_capability_publisher=publisher,
    )
    await runtime.start()
    await transport.emit(
        command_topic(NODE_ID.root, "register-ack"),
        _ack(registration.pending_register_message_id, SESSION_ID.root),
    )
    await asyncio.sleep(0.05)
    return runtime, registration, transport


async def _stop_runtime(runtime: AgentRuntime) -> None:
    await runtime.stop()


def test_agent_runtime_publishes_v2_snapshot_after_register_ack(tmp_path) -> None:
    runtime, registration, transport = asyncio.run(_run_runtime(tmp_path))
    try:
        assert registration.registered is True
        assert v2_command_topic(NODE_ID.root, "agent.diagnostics.request") in transport.subscriptions
        assert any(
            topic == v2_event_topic(NODE_ID.root, "capability.snapshot")
            for topic, _, _ in transport.published
        )
    finally:
        asyncio.run(_stop_runtime(runtime))
