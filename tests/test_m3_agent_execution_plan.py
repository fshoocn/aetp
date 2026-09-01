"""M3 Agent V2 execution.plan 预检测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from aetp_protocol.execution import ExecutionPlan, ExecutorRef, PlanResourceBinding
from aetp_protocol.ids import BusinessId, MessageId, PluginId, SemVer, SessionId, Sha256, TraceId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionAck
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender, parse_v2_message

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.application.services.v2_execution_plan_controller import AgentV2ExecutionPlanController
from agent.config import AgentSettings
from agent.plugins.v2_installer import V2PluginInstaller
from agent.plugins.v2_registry import AgentV2PluginRegistry
from common.transport import MqttMessage, Transport
from master.application.services.plan_lease_service import with_plan_hash
from tests.test_agent_v2_plugin_sync import _package
from tests.test_m3_plan_lease import NOW, _plan
from tests.test_v2_plugin_archive import _archive

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
MASTER_ID = stable_id("aetp-master")


class PlanTransport:
    connected = True

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        del topic, payload, qos


def _controller(tmp_path) -> tuple[AgentV2ExecutionPlanController, SQLiteLedger, AgentV2CapabilityPublisher]:
    settings = AgentSettings(
        node_id=NODE_ID.root,
        name="Bench 01",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-01",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentV2PluginRegistry(settings.plugin_dir)
    content = _archive()
    package = _package(content)
    installed = V2PluginInstaller(settings.plugin_dir, fetcher=lambda _: content).install(package)
    registry.register(installed)
    publisher = AgentV2CapabilityPublisher(
        cast(Transport, PlanTransport()),
        settings,
        registry,
        capability_scanner=lambda: None,
    )
    return (
        AgentV2ExecutionPlanController(
            NODE_ID,
            ledger,
            publisher,
            registry,
            master_id="aetp-master",
            now=lambda: NOW,
        ),
        ledger,
        publisher,
    )


def _valid_plan() -> ExecutionPlan:
    plan = _plan().model_copy(
        update={
            "executor": ExecutorRef(
                plugin_id=PluginId("org.example.executor"),
                version=SemVer("2.0.0"),
            ),
            "plugin_package": None,
            "resource_bindings": (),
        }
    )
    return with_plan_hash(plan)


def _command(plan: ExecutionPlan, *, message_id: str = "plan-message-0001") -> MqttMessage:
    envelope = V2Envelope(
        message_id=MessageId(message_id),
        sent_at=NOW,
        sender=V2Sender(kind="master", id=MASTER_ID, session_id=SessionId("master-session-01")),
        message_type=MessageType.EXECUTION_PLAN.value,
        trace_id=TraceId("plan-trace-000001"),
        payload=plan.model_dump(mode="json"),
    )
    return MqttMessage(
        topic=v2_command_topic(NODE_ID.root, "execution.plan"),
        payload=envelope.model_dump_json().encode("utf-8"),
    )


def _ack(ledger: SQLiteLedger) -> ExecutionAck:
    outbox = ledger.claim_due_outbox(
        10,
        (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None),
    )[-1]
    _, payload = parse_v2_message(outbox.payload)
    assert isinstance(payload, ExecutionAck)
    return payload


def test_agent_execution_plan_accepts_after_fixed_reference_precheck(tmp_path) -> None:
    controller, ledger, _publisher = _controller(tmp_path)
    plan = _valid_plan()

    assert asyncio.run(controller.handle(_command(plan), SESSION_ID)) is True

    run = ledger.get_run(plan.run_id.root)
    assert run is not None
    assert run.plan_id == plan.plan_id.root
    ack = _ack(ledger)
    assert ack.accepted is True
    assert ack.plan_hash == plan.plan_hash
    assert ack.plan_id == plan.plan_id


def test_agent_execution_plan_rejects_hash_session_and_expired_lease(tmp_path) -> None:
    controller, ledger, _publisher = _controller(tmp_path)
    plan = _valid_plan()

    tampered = plan.model_copy(update={"plan_hash": Sha256("0" * 64)})
    assert asyncio.run(controller.handle(_command(tampered), SESSION_ID)) is True
    tampered_ack = _ack(ledger)
    assert tampered_ack.code is not None
    assert tampered_ack.code.root == "EXECUTION_PLAN_INVALID"

    wrong_session = _command(plan, message_id="plan-message-0002")
    assert asyncio.run(controller.handle(wrong_session, SessionId("session-00000002"))) is True
    wrong_session_ack = _ack(ledger)
    assert wrong_session_ack.code is not None
    assert wrong_session_ack.code.root == "STALE_SESSION"

    expired_binding = PlanResourceBinding(
        lease_id=stable_id("expired-lease"),
        resource_id=BusinessId("01J00000000000000000000008"),
        resource_type="can",
        lease_revision=1,
        expires_at=NOW - timedelta(seconds=1),
    )
    expired = with_plan_hash(plan.model_copy(update={"resource_bindings": (expired_binding,)}))
    assert asyncio.run(controller.handle(_command(expired, message_id="plan-message-0003"), SESSION_ID)) is True
    expired_ack = _ack(ledger)
    assert expired_ack.code is not None
    assert expired_ack.code.root == "RESOURCE_LEASE_EXPIRED"
    assert ledger.get_run(plan.run_id.root) is None


def test_agent_execution_plan_duplicate_is_idempotent_and_conflicting_plan_rejected(tmp_path) -> None:
    controller, ledger, _publisher = _controller(tmp_path)
    plan = _valid_plan()

    assert asyncio.run(controller.handle(_command(plan), SESSION_ID)) is True
    first = _ack(ledger)
    assert first.accepted is True

    assert asyncio.run(controller.handle(_command(plan, message_id="plan-message-0002"), SESSION_ID)) is True
    repeated = _ack(ledger)
    assert repeated.accepted is True

    conflicting = with_plan_hash(
        plan.model_copy(
            update={
                "plan_id": BusinessId("01J00000000000000000000012"),
                "attempt_id": BusinessId("01J00000000000000000000013"),
            }
        )
    )
    assert asyncio.run(controller.handle(_command(conflicting, message_id="plan-message-0003"), SESSION_ID)) is True
    rejected = _ack(ledger)
    assert rejected.accepted is False
    assert rejected.code is not None
    assert rejected.code.root == "STALE_ATTEMPT"
