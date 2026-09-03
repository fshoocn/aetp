"""Agent  execution.reconcile 对账消息测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

from aetp_protocol.envelope import Envelope, Sender, parse_message
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionReconcile, ExecutionReconcileResult
from aetp_protocol.topics import command_topic, event_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.application.services.reconcile_service import ReconcileService
from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus
from agent.plugins.registry import PluginRegistry
from common.transport import MqttMessage, Transport

NODE_ID = BusinessId("01J00000000000000000000050")
SESSION_ID = SessionId("session-00000050")


class _Transport:
    connected = True

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        del topic, payload, qos


def _service(tmp_path) -> tuple[ReconcileService, SQLiteLedger]:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    publisher = CapabilityPublisher(
        cast(Transport, _Transport()),
        AgentSettings(
            node_id=NODE_ID.root,
            name="Bench 50",
            master_id="aetp-master",
            mqtt_use_tls=False,
            plugin_dir=tmp_path / "plugins",
        ),
        PluginRegistry(),
    )
    return ReconcileService(NODE_ID, ledger, publisher), ledger


def test_reconcile_serializes_terminal_local_attempt(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    ledger.claim_run(
        "01J00000000000000000000051",
        1,
        plan_id="01J00000000000000000000052",
        shard_id="01J00000000000000000000053",
        attempt_id="01J00000000000000000000054",
        plan_hash="a" * 64,
    )
    run = ledger.get_run("01J00000000000000000000051")
    assert run is not None
    run.status = AgentRunStatus.SUCCEEDED
    run.result_summary = {"passed": True, "metrics": {"total": 1}}
    run.last_progress_sequence = 7
    ledger.update_run(run)

    outbox_id = service.enqueue(SESSION_ID)
    entry = ledger.get_outbox(outbox_id)
    assert entry is not None
    envelope, payload = parse_message(entry.payload)
    assert envelope.message_type == MessageType.EXECUTION_RECONCILE.value
    assert isinstance(payload, ExecutionReconcile)
    assert payload.node_id == NODE_ID
    assert payload.attempts[0].state == "succeeded"
    assert payload.attempts[0].last_progress_sequence == 7
    assert payload.attempts[0].result is not None and payload.attempts[0].result.passed is True


def test_reconcile_result_requires_pending_correlation(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    outbox_id = service.enqueue(SESSION_ID)
    entry = ledger.get_outbox(outbox_id)
    assert entry is not None
    envelope, _payload = parse_message(entry.payload)
    response = Envelope(
        message_id=MessageId("reconcile-result-0000001"),
        correlation_id=envelope.message_id,
        sent_at=datetime.now(UTC),
        sender=Sender(
            kind="master",
            id=stable_id("aetp-master"),
            session_id=SessionId("master-session-000050"),
        ),
        message_type=MessageType.EXECUTION_RECONCILE_RESULT.value,
        trace_id=TraceId("reconcile-trace-000001"),
        payload=ExecutionReconcileResult(node_id=NODE_ID, accepted=True).model_dump(mode="json"),
    )
    assert service.handle_result(
        MqttMessage(
            topic=command_topic(NODE_ID.root, "execution.reconcile_result"),
            payload=json.dumps(response.model_dump(mode="json")).encode("utf-8"),
        ),
        SESSION_ID,
    ) is True
    assert service.handle_result(
        MqttMessage(
            topic=event_topic(NODE_ID.root, "execution.reconcile"),
            payload=json.dumps(response.model_dump(mode="json")).encode("utf-8"),
        ),
        SESSION_ID,
    ) is False
