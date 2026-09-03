"""P5.3：Agent MQTT Transport 参数与 LWT 契约测试。"""

from __future__ import annotations

import json

from aetp_protocol.envelope import Envelope, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import Presence

from agent.adapters.mqtt.transport import AgentMqttTransport
from agent.config import AgentSettings


def test_agent_transport_uses_aiomqtt_identifier_and_envelope_lwt() -> None:
    settings = AgentSettings(
        node_id="01J00000000000000000000000",
        name="bench",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-001",
        mqtt_use_tls=False,
    )
    transport = AgentMqttTransport(settings)
    kwargs = transport._client_kwargs()

    assert kwargs["identifier"] == "aetp-agent-bench-001"
    assert "client_id" not in kwargs
    will = kwargs["will"]
    envelope = Envelope.model_validate(json.loads(will.payload))
    assert envelope.message_type == MessageType.PRESENCE.value
    assert envelope.sender.kind is SenderKind.AGENT
    assert envelope.sender.id.root == "01J00000000000000000000000"
    assert envelope.sender.session_id.root == transport.session_id
    payload = Presence.model_validate(envelope.payload)
    assert payload.node_id.root == "01J00000000000000000000000"
    assert payload.reason == "unexpected_disconnect"
