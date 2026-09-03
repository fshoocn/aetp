"""M4 V2 execution.cancel 消息测试。"""

from __future__ import annotations

import asyncio
import json

from aetp_protocol.envelope import parse_message
from aetp_protocol.ids import stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionCancel
from aetp_protocol.plan_hash import with_plan_hash
from aetp_protocol.topics import command_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.application.services.execution_plan_controller import ExecutionPlanController
from agent.application.services.execution_service import ExecutionService
from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus
from agent.plugins.registry import PluginRegistry
from common.transport import MqttMessage
from master.application.services.plan_lease_service import PlanLeaseService
from master.application.services.plan_materialization_service import PlanMaterializationService
from tests.test_agent_capability_publisher import FakeTransport
from tests.test_m3_plan_lease import NOW, SESSION_ID, _plan
from tests.test_m3_plan_materialization import _seed_context

NODE_ID = stable_id("m4-cancel-node")


def test_master_cancel_command_is_typed_and_agent_marks_cancel_requested(client, tmp_path) -> None:
    container = client.app.state.container
    plan = with_plan_hash(_plan().model_copy(update={"node_id": NODE_ID}))
    _seed_context(container, plan)
    materializer = PlanMaterializationService(
        container.uow_factory(),
        PlanLeaseService(container.uow_factory(), now=lambda: NOW),
    )
    materializer.materialize(plan)
    master_outbox = container.execution_service().request_cancel(plan.plan_id, reason="user requested")
    master_envelope, master_payload = parse_message(master_outbox.payload)
    assert master_envelope.message_type == MessageType.EXECUTION_CANCEL.value
    assert isinstance(master_payload, ExecutionCancel)
    assert master_payload.request.reason == "user requested"
    assert master_outbox.topic == command_topic(NODE_ID.root, "execution.cancel")

    settings = AgentSettings(
        node_id=NODE_ID.root,
        name="Bench 01",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-01",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    execution = ExecutionService(settings, ledger)
    publisher = CapabilityPublisher(
        transport=FakeTransport(),
        settings=settings,
        registry=PluginRegistry(),
    )
    controller = ExecutionPlanController(
        NODE_ID,
        ledger,
        publisher,
        PluginRegistry(),
        is_registered=lambda: True,
        execution_service=execution,
    )
    ledger.claim_run(plan.run_id.root, plan.attempt_no, plan_id=plan.plan_id.root)

    assert asyncio.run(
        controller.handle_cancel(
            MqttMessage(
                topic=master_outbox.topic,
                payload=json.dumps(master_outbox.payload).encode("utf-8"),
            ),
            SESSION_ID,
        )
    ) is True
    run = ledger.get_run(plan.run_id.root)
    assert run is not None
    assert run.cancelled is True
    assert run.status is AgentRunStatus.CLAIMED
