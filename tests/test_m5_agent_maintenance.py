"""M5 Agent 远程维护命令测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aetp_protocol.capabilities import AgentMaintenanceState
from aetp_protocol.envelope import SenderKind
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, stable_id
from aetp_protocol.logs import LogLevel
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    LogLevelUpdateRequest,
    LogLevelUpdateResult,
    MaintenanceDrainRequest,
    MaintenanceDrainResult,
    MaintenanceRestartRequest,
    MaintenanceRestartResult,
)
from aetp_protocol.topics import v2_command_topic, v2_event_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender, parse_v2_message

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.agent_log_facade import AgentLogFacade
from agent.application.services.maintenance_controller import AgentMaintenanceController
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.config import AgentSettings
from agent.plugins.v2_registry import AgentV2PluginRegistry
from common.transport import MqttMessage

NODE_ID = BusinessId("01J000000000000000000000A0")
SESSION_ID = SessionId("session-00000100")
MASTER_SESSION_ID = SessionId("session-master-01")


class _Transport:
    connected = True

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int]] = []

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.published.append((topic, payload, qos))


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(
        node_id=NODE_ID.root,
        name="maintenance-agent",
        master_id="aetp-master",
        mqtt_client_id="maintenance-agent",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
        log_file=tmp_path / "logs" / "agent.jsonl",
    )


def _envelope(message_type: MessageType, payload: object, message_id: str) -> MqttMessage:
    envelope = V2Envelope(
        message_id=MessageId(message_id),
        sent_at="2026-09-02T08:00:00Z",
        sender=V2Sender(
            kind=SenderKind.MASTER,
            id=stable_id("aetp-master"),
            session_id=MASTER_SESSION_ID,
        ),
        message_type=message_type.value,
        trace_id=TraceId("maintenance-trace-0001"),
        payload=payload.model_dump(mode="json"),
    )
    segment = {
        MessageType.AGENT_LOG_LEVEL_UPDATE: "agent.log.level.update",
        MessageType.AGENT_MAINTENANCE_DRAIN: "agent.maintenance.drain",
        MessageType.AGENT_MAINTENANCE_RESTART: "agent.maintenance.restart",
    }[message_type]
    return MqttMessage(
        topic=v2_command_topic(NODE_ID.root, segment),
        payload=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
    )


def _controller(
    tmp_path: Path,
    *,
    active_attempt_count=None,
    restart=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = _settings(tmp_path)
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    transport = _Transport()
    publisher = AgentV2CapabilityPublisher(
        transport,
        settings,
        AgentV2PluginRegistry(tmp_path / "plugins"),
    )
    facade = AgentLogFacade(settings, ledger)
    controller = AgentMaintenanceController(
        NODE_ID,
        ledger,
        publisher,
        facade,
        active_attempt_count=active_attempt_count,
        is_registered=lambda: True,
        sleep=lambda _delay: asyncio.sleep(0),
        restart=restart,
    )
    return controller, publisher, facade, transport, ledger


def test_log_level_update_publishes_result_and_is_idempotent(tmp_path: Path) -> None:
    controller, _publisher, facade, transport, ledger = _controller(tmp_path)
    request = LogLevelUpdateRequest(
        node_id=NODE_ID,
        operation_id=BusinessId("01J00000000000000000000011"),
        expected_session_id=SESSION_ID,
        component="agent.runtime",
        level=LogLevel.DEBUG,
    )
    message = _envelope(MessageType.AGENT_LOG_LEVEL_UPDATE, request, "maintenance-message-01")

    try:
        assert asyncio.run(controller.handle(message, SESSION_ID)) is True
        assert asyncio.run(controller.handle(message, SESSION_ID)) is True
        assert facade._component_levels["agent.runtime"][0] == 10
        result_messages = [
            item
            for item in transport.published
            if item[0] == v2_event_topic(NODE_ID.root, "agent.log.level.updated")
        ]
        assert len(result_messages) == 2
        _envelope_result, result = parse_v2_message(json.loads(result_messages[-1][1].decode("utf-8")))
        assert isinstance(result, LogLevelUpdateResult)
        assert result.accepted is True
        assert ledger.get_run("not-a-run") is None
    finally:
        facade.close()


def test_drain_waits_until_active_runs_finish(tmp_path: Path) -> None:
    counts = iter((1, 0))
    controller, publisher, facade, transport, _ledger = _controller(
        tmp_path,
        active_attempt_count=lambda: next(counts),
    )
    request = MaintenanceDrainRequest(
        node_id=NODE_ID,
        operation_id=BusinessId("01J00000000000000000000012"),
        expected_session_id=SESSION_ID,
        drain_timeout_s=10,
    )

    try:
        assert asyncio.run(
            controller.handle(
                _envelope(MessageType.AGENT_MAINTENANCE_DRAIN, request, "maintenance-message-02"),
                SESSION_ID,
            )
        ) is True
        assert publisher.maintenance_state is AgentMaintenanceState.IDLE
        result_messages = [
            item
            for item in transport.published
            if item[0] == v2_event_topic(NODE_ID.root, "agent.maintenance.drain.result")
        ]
        _envelope_result, result = parse_v2_message(json.loads(result_messages[-1][1].decode("utf-8")))
        assert isinstance(result, MaintenanceDrainResult)
        assert result.accepted is True
        assert result.active_attempt_count == 0
    finally:
        facade.close()


def test_drain_timeout_and_restart_callback(tmp_path: Path) -> None:
    restart_called: list[bool] = []
    controller, publisher, facade, transport, _ledger = _controller(
        tmp_path,
        active_attempt_count=lambda: 1,
        restart=lambda: restart_called.append(True),
    )
    drain = MaintenanceDrainRequest(
        node_id=NODE_ID,
        operation_id=BusinessId("01J00000000000000000000013"),
        expected_session_id=SESSION_ID,
        drain_timeout_s=0,
    )
    restart = MaintenanceRestartRequest(
        node_id=NODE_ID,
        operation_id=BusinessId("01J00000000000000000000014"),
        expected_session_id=SESSION_ID,
        drain_timeout_s=0,
    )

    try:
        assert asyncio.run(
            controller.handle(
                _envelope(MessageType.AGENT_MAINTENANCE_DRAIN, drain, "maintenance-message-03"),
                SESSION_ID,
            )
        ) is True
        _envelope_result, drain_result = parse_v2_message(
            json.loads(transport.published[-1][1].decode("utf-8"))
        )
        assert isinstance(drain_result, MaintenanceDrainResult)
        assert drain_result.accepted is False
        assert drain_result.code is not None and drain_result.code.root == "AGENT_MAINTENANCE"

        controller, publisher, facade, transport, _ledger = _controller(
            tmp_path / "restart",
            active_attempt_count=lambda: 0,
            restart=lambda: restart_called.append(True),
        )
        assert asyncio.run(
            controller.handle(
                _envelope(MessageType.AGENT_MAINTENANCE_RESTART, restart, "maintenance-message-04"),
                SESSION_ID,
            )
        ) is True
        assert restart_called == [True]
        assert publisher.maintenance_state is AgentMaintenanceState.RESTARTING
        restart_messages = [
            item
            for item in transport.published
            if item[0] == v2_event_topic(NODE_ID.root, "agent.maintenance.restart.result")
        ]
        _envelope_result, restart_result = parse_v2_message(
            json.loads(restart_messages[-1][1].decode("utf-8"))
        )
        assert isinstance(restart_result, MaintenanceRestartResult)
        assert restart_result.accepted is True
    finally:
        facade.close()
