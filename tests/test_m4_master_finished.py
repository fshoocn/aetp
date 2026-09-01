"""M4 Master execution.finished 终态投影测试。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from aetp_protocol.execution import CaseResult, CaseStatus, ExecutionResult, ExecutionStatus, LeaseState
from aetp_protocol.ids import MessageId, TraceId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionFinished
from aetp_protocol.plan_hash import with_plan_hash
from aetp_protocol.topics import v2_event_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from common.transport import MqttMessage
from master.application.services.plan_lease_service import PlanLeaseService
from master.application.services.v2_plan_materialization_service import V2PlanMaterializationService
from master.domain.enums import RunStatus, ShardAttemptStatus, ShardStatus
from tests.test_m3_plan_lease import NOW, _plan
from tests.test_m3_plan_materialization import _seed_context

NODE_ID = stable_id("m4-node")


def _finished_message(plan, *, message_id: str) -> MqttMessage:
    finished = ExecutionFinished(
        run_id=plan.run_id,
        shard_id=plan.shard_id,
        attempt_id=plan.attempt_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        result=ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            passed=True,
            case_results=(
                CaseResult(
                    case_key=plan.case_keys[0],
                    status=CaseStatus.PASSED,
                    duration_ms=123,
                ),
            ),
        ),
        finished_at=NOW + timedelta(minutes=1),
    )
    envelope = V2Envelope(
        message_id=MessageId(message_id),
        correlation_id=MessageId("execution-plan-0001"),
        sent_at=NOW + timedelta(minutes=1),
        sender=V2Sender(kind="agent", id=plan.node_id, session_id=plan.target_session_id),
        message_type=MessageType.EXECUTION_FINISHED.value,
        trace_id=TraceId("m4-finished-trace-0001"),
        payload=finished.model_dump(mode="json"),
    )
    return MqttMessage(
        topic=v2_event_topic(plan.node_id.root, "execution.finished"),
        payload=envelope.model_dump_json().encode("utf-8"),
    )


def test_master_projects_finished_and_releases_v2_leases(client) -> None:
    container = client.app.state.container
    plan = with_plan_hash(_plan().model_copy(update={"node_id": stable_id("m4-node")}))
    _seed_context(container, plan)
    plan_leases = PlanLeaseService(container.uow_factory(), now=lambda: NOW)
    materializer = V2PlanMaterializationService(container.uow_factory(), plan_leases)
    materializer.materialize(plan)

    message = _finished_message(plan, message_id="execution-finished-0001")
    router = container.message_router()
    assert asyncio.run(router.handle(message)) is True
    assert asyncio.run(router.handle(message)) is True

    with container.uow_factory()() as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(plan.attempt_id.root)
        shard = uow.run_shards.get_by_shard_id(plan.shard_id.root)
        run = uow.task_runs.get_by_run_id(plan.run_id.root)
        lease = uow.resource_leases.get_by_lease_id(plan.resource_bindings[0].lease_id)
        case_results = uow.run_case_results.list_by_shard(plan.run_id.root, plan.shard_id.root)
        assert attempt is not None and attempt.status is ShardAttemptStatus.SUCCEEDED
        assert shard is not None and shard.status is ShardStatus.SUCCEEDED
        assert run is not None and run.status is RunStatus.SUCCEEDED
        assert lease is not None and lease.lease.state is LeaseState.RELEASED
        assert len(case_results) == 1
        assert case_results[0].case_key == plan.case_keys[0]
        assert case_results[0].status.value == CaseStatus.PASSED.value
