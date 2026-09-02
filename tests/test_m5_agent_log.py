"""M5 Agent 结构化日志门面、spool、批次和 ACK 测试。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, stable_id
from aetp_protocol.logs import LogLevel
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import AgentLogReceived
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender, parse_v2_message

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.agent_log_facade import AgentLogFacade
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.config import AgentSettings
from agent.plugins.v2_registry import AgentV2PluginRegistry
from common.transport import MqttMessage, Transport

NODE_ID = "01J000000000000000000000A0"
SESSION_ID = SessionId("session-00000100")


class _Transport:
    connected = True

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        del topic, payload, qos


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(
        node_id=NODE_ID,
        name="log-bench",
        master_id="aetp-master",
        mqtt_client_id="log-agent",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
        log_file=tmp_path / "logs" / "agent.jsonl",
        task_log_batch_size=100,
    )


def test_agent_log_facade_writes_jsonl_and_builds_batch(tmp_path: Path) -> None:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    facade = AgentLogFacade(_settings(tmp_path), ledger)
    logger = logging.getLogger("m5.test.component")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(facade)
    try:
        logger.info(
            "request password=super-secret",
            extra={
                "aetp_context": {"node_id": NODE_ID},
                "aetp_detail": {"password": "super-secret", "ok": True},
            },
        )
    finally:
        logger.removeHandler(facade)
        facade.close()

    batch = facade.build_batch(SESSION_ID)
    assert batch is not None
    assert batch.node_id == BusinessId(NODE_ID)
    assert batch.events[0].sequence == 1
    assert "super-secret" not in batch.events[0].message
    assert batch.events[0].detail["password"] == "[REDACTED]"
    log_file = _settings(tmp_path).structured_log_file or _settings(tmp_path).log_file.with_suffix(".jsonl")
    assert log_file.is_file()
    assert "super-secret" not in log_file.read_text(encoding="utf-8")


def test_agent_log_batch_is_kept_until_master_ack(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    facade = AgentLogFacade(settings, ledger)
    record = logging.LogRecord("m5.agent", logging.INFO, __file__, 1, "hello", (), None)
    facade.handle(record)
    batch = facade.build_batch(SESSION_ID)
    assert batch is not None

    publisher = AgentV2CapabilityPublisher(
        cast(Transport, _Transport()),
        settings,
        AgentV2PluginRegistry(),
    )
    outbox_id = publisher.enqueue_agent_log_batch(ledger, batch, SESSION_ID)
    entry = ledger.get_outbox(outbox_id)
    assert entry is not None
    envelope, payload = parse_v2_message(entry.payload)
    assert envelope.message_type == MessageType.AGENT_LOG_BATCH.value
    assert payload == batch
    assert facade.pending_count() == 1

    ack = V2Envelope(
        message_id=MessageId("m5-log-ack-message-01"),
        correlation_id=envelope.message_id,
        sent_at=datetime.now(UTC),
        sender=V2Sender(
            kind="master",
            id=stable_id("aetp-master"),
            session_id=SessionId("master-session-0001"),
        ),
        message_type=MessageType.AGENT_LOG_RECEIVED.value,
        trace_id=TraceId("m5-log-ack-trace-0001"),
        payload=AgentLogReceived(
            node_id=BusinessId(NODE_ID),
            session_id=SESSION_ID,
            first_sequence=batch.first_sequence,
            last_sequence=batch.events[-1].sequence,
        ).model_dump(mode="json"),
    )
    assert publisher.handle_agent_log_received(
        MqttMessage(
            topic=v2_command_topic(NODE_ID, "agent.log.received"),
            payload=json.dumps(ack.model_dump(mode="json")).encode("utf-8"),
        ),
        SESSION_ID,
        facade,
    ) is True
    assert facade.pending_count() == 0


def test_component_debug_override_reaches_facade_below_global_level(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    facade = AgentLogFacade(settings, ledger, default_level="INFO")
    root_logger = logging.getLogger()
    component_logger = logging.getLogger("agent.override")
    old_root_level = root_logger.level
    old_component_level = component_logger.level
    root_logger.setLevel(logging.INFO)
    component_logger.setLevel(logging.NOTSET)
    root_logger.addHandler(facade)
    try:
        facade.update_level("agent.override", LogLevel.DEBUG)
        component_logger.debug("debug override reached facade")
        batch = facade.build_batch(SESSION_ID)
        assert batch is not None
        assert batch.events[0].level is LogLevel.DEBUG
    finally:
        root_logger.removeHandler(facade)
        root_logger.setLevel(old_root_level)
        component_logger.setLevel(old_component_level)
        facade.close()


def test_agent_log_spool_evicts_debug_before_info_and_warn(tmp_path: Path) -> None:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}", max_spool_bytes=1000)
    settings = _settings(tmp_path)
    facade = AgentLogFacade(settings, ledger, default_level="DEBUG")
    records = [
        logging.LogRecord("agent.m5", logging.WARNING, __file__, 1, "warn event", (), None),
        logging.LogRecord("agent.m5", logging.INFO, __file__, 1, "info event", (), None),
        logging.LogRecord("agent.m5", logging.DEBUG, __file__, 1, "debug event", (), None),
        logging.LogRecord("agent.m5", logging.ERROR, __file__, 1, "error event", (), None),
    ]
    try:
        for record in records:
            facade.handle(record)
        levels = [entry.event.level for entry in ledger.list_pending_agent_logs(20)]
        assert LogLevel.ERROR in levels
        assert LogLevel.WARN in levels
        assert LogLevel.DEBUG not in levels
    finally:
        facade.close()
