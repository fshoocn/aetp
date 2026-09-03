"""AgentRuntime 注册、心跳和连接生命周期测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from aetp_protocol.capabilities import NodeCapabilities
from aetp_protocol.envelope import Envelope, Sender
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import NodeRegisterAck
from aetp_protocol.topics import event_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.runtime import AgentRuntime
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.application.services.execution_service import ExecutionService
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.config import AgentSettings
from agent.plugins.installer import PluginInstaller
from agent.plugins.registry import PluginRegistry
from common.transport import MqttMessage

NODE_ID = "01J00000000000000000000000"
SESSION_ID = "session-current-0001"


class RuntimeTransport:
    connected = False

    def __init__(self) -> None:
        self.message_handler: Callable[[MqttMessage], Awaitable[None]] | None = None
        self.connection_handler: Callable[[bool, str], Awaitable[None]] | None = None
        self.subscriptions: list[str] = []
        self.published: list[tuple[str, bytes, int]] = []
        self.session_id = SESSION_ID

    def on_message(self, handler: Callable[[MqttMessage], Awaitable[None]]) -> None:
        self.message_handler = handler

    def on_connection_change(self, handler: Callable[[bool, str], Awaitable[None]]) -> None:
        self.connection_handler = handler

    async def connect(self) -> None:
        self.connected = True
        assert self.connection_handler is not None
        await self.connection_handler(True, self.session_id)

    async def disconnect(self) -> None:
        if not self.connected:
            return
        self.connected = False
        assert self.connection_handler is not None
        await self.connection_handler(False, self.session_id)

    async def subscribe(self, topics: list[str]) -> None:
        self.subscriptions = list(topics)

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.published.append((topic, payload, qos))

    async def emit(self, topic: str, payload: bytes) -> None:
        assert self.message_handler is not None
        await self.message_handler(MqttMessage(topic=topic, payload=payload))


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(
        node_id=NODE_ID,
        name="Bench 01",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-01",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
        script_cache_dir=tmp_path / "scripts",
        heartbeat_interval_s=1,
        registration_timeout_s=1,
    ).validate()


def _register_ack(correlation_id: MessageId, session_id: SessionId) -> bytes:
    envelope = Envelope(
        message_id=MessageId("master-ack-current-01"),
        correlation_id=correlation_id,
        sent_at=datetime.now(UTC),
        sender=Sender(
            kind="master",
            id=stable_id("aetp-master"),
            session_id=SessionId("master-session-0001"),
        ),
        message_type=MessageType.NODE_REGISTER_ACK.value,
        trace_id=TraceId("trace-registration-01"),
        payload=NodeRegisterAck(
            node_id=BusinessId(NODE_ID),
            session_id=session_id,
            accepted=True,
        ).model_dump(mode="json"),
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


def test_agent_settings_use_isolated_defaults(tmp_path: Path) -> None:
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "AETP_AGENT_NODE_ID=\n"
        "AETP_AGENT_NAME=Bench\n",
        encoding="utf-8",
    )

    settings = AgentSettings.from_env_file(env_file).validate()

    assert settings == settings.validate()
    assert len(settings.node_id) == 26
    assert settings.ledger_url == "sqlite:///data/agent-runtime.db"
    assert settings.plugin_dir == (tmp_path / "data" / "plugins").resolve()
    assert settings.script_cache_dir == (tmp_path / "data" / "scripts").resolve()


def test_agent_runtime_registers_and_publishes_heartbeat(tmp_path: Path) -> None:
    transport = RuntimeTransport()
    settings = _settings(tmp_path)
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = PluginRegistry(settings.plugin_dir)
    publisher = CapabilityPublisher(
        transport,
        settings,
        registry,
        capability_scanner=lambda: NodeCapabilities(),
    )
    runtime = AgentRuntime(
        settings,
        transport,
        ledger,
        execution_service=ExecutionService(settings, ledger),
        capability_publisher=publisher,
        plugin_installer=PluginInstaller(settings.plugin_dir),
        plugin_registry=registry,
        executor_resolver=lambda _plan: object(),
        resource_providers=ResourceProviderRegistry(),
    )

    async def scenario() -> None:
        await runtime.start()
        await asyncio.sleep(0.05)

        assert transport.subscriptions
        assert all(topic.startswith("aetp/v2/") for topic in transport.subscriptions)
        assert publisher.pending_register_message_id is not None
        assert not any(topic == event_topic(NODE_ID, "heartbeat") for topic, _, _ in transport.published)

        await transport.emit(
            publisher.register_ack_topic(),
            _register_ack(publisher.pending_register_message_id, SessionId(SESSION_ID)),
        )
        await asyncio.sleep(0.05)

        assert publisher.registered is True
        assert any(topic == event_topic(NODE_ID, "heartbeat") for topic, _, _ in transport.published)
        await runtime.stop()

    asyncio.run(scenario())
