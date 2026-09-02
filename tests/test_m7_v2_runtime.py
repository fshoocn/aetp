"""M7-2：V2-only Master/Agent Runtime smoke 测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from aetp_protocol.capabilities import NodeCapabilities
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import NodeRegisterAck, Presence
from aetp_protocol.topics import v2_event_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender, parse_v2_message

from agent.adapters.mqtt.transport import AgentMqttTransport
from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.runtime import AgentRuntime
from agent.application.services.execution_service import ExecutionService
from agent.application.services.registration_service import RegistrationService
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.config import AgentSettings
from agent.plugins.v2_installer import V2PluginInstaller
from agent.plugins.v2_registry import AgentV2PluginRegistry
from tests.test_agent_runtime import RuntimeTransport

NODE_ID = "01J00000000000000000000000"
SESSION_ID = "session-v2-000001"


def test_agent_v2_profile_uses_ulid_and_isolated_defaults(tmp_path: Path) -> None:
    env_file = tmp_path / "agent-v2.env"
    env_file.write_text(
        "AETP_AGENT_PROFILE=v2\n"
        "AETP_AGENT_NODE_ID=\n"
        "AETP_AGENT_NAME=V2 Bench\n",
        encoding="utf-8",
    )

    settings = AgentSettings.from_env_file(env_file).validate()

    assert settings.v2_only is True
    assert len(settings.node_id) == 26
    assert settings.ledger_url == "sqlite:///data-v2/agent-v2.db"
    assert settings.plugin_dir == (tmp_path / "data-v2" / "plugins").resolve()
    assert settings.script_cache_dir == (tmp_path / "data-v2" / "scripts").resolve()


def test_agent_v2_runtime_subscribes_and_publishes_v2_heartbeat(tmp_path: Path) -> None:
    transport = RuntimeTransport()
    transport.session_id = SESSION_ID
    settings = AgentSettings(
        profile="v2",
        node_id=NODE_ID,
        name="V2 Bench",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-v2",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
        script_cache_dir=tmp_path / "scripts",
        heartbeat_interval_s=1,
        registration_timeout_s=1,
    ).validate()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentV2PluginRegistry(settings.plugin_dir)
    publisher = AgentV2CapabilityPublisher(
        transport,
        settings,
        registry,
        capability_scanner=lambda: NodeCapabilities(),
    )
    registration = RegistrationService(
        transport,
        ledger,
        settings,
        session_id=SESSION_ID,
    )
    runtime = AgentRuntime(
        settings,
        transport,
        ledger,
        registration,
        execution_service=ExecutionService(settings, ledger),
        v2_capability_publisher=publisher,
        v2_plugin_installer=V2PluginInstaller(settings.plugin_dir),
        v2_plugin_registry=registry,
        v2_executor_resolver=lambda _plan: object(),
        resource_providers=ResourceProviderRegistry(),
    )

    async def scenario() -> None:
        await runtime.start()
        assert all(topic.startswith("aetp/v2/") for topic in transport.subscriptions)
        assert not any(topic.startswith("aetp/v1/") for topic in transport.subscriptions)
        assert publisher.pending_register_message_id is not None
        ack = V2Envelope(
            message_id=MessageId("master-ack-v2-000001"),
            correlation_id=publisher.pending_register_message_id,
            sent_at=datetime.now(UTC),
            sender=V2Sender(
                kind="master",
                id=stable_id(settings.master_id),
                session_id=SessionId("master-session-v2"),
            ),
            message_type=MessageType.NODE_REGISTER_ACK.value,
            trace_id=TraceId("trace-v2-000001-x"),
            payload=NodeRegisterAck(
                node_id=BusinessId(NODE_ID),
                session_id=SessionId(SESSION_ID),
                accepted=True,
            ).model_dump(mode="json"),
        )
        await transport.emit(
            publisher.register_ack_topic(),
            json.dumps(ack.model_dump(mode="json")).encode("utf-8"),
        )
        await asyncio.sleep(0.05)
        assert publisher.v2_registered is True
        assert any(topic == v2_event_topic(NODE_ID, "heartbeat") for topic, _, _ in transport.published)
        await runtime.stop()

    asyncio.run(scenario())


def test_agent_v2_transport_uses_v2_presence_lwt() -> None:
    settings = AgentSettings(
        profile="v2",
        node_id=NODE_ID,
        name="V2 Bench",
        mqtt_client_id="aetp-agent-v2",
        mqtt_use_tls=False,
    ).validate()
    transport = AgentMqttTransport(settings)

    will = transport._client_kwargs()["will"]
    envelope, payload = parse_v2_message(json.loads(transport._lwt_payload().decode("utf-8")))

    assert will.topic == v2_event_topic(NODE_ID, "presence")
    assert isinstance(payload, Presence)
    assert envelope.message_type == MessageType.PRESENCE.value
    assert envelope.sender.id == BusinessId(NODE_ID)
