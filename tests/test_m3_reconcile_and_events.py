"""M3 execution 运行事件和重连对账测试。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from aetp_protocol.envelope import Envelope, Sender, parse_message
from aetp_protocol.execution import CaseResult, CaseStatus, ExecutionResult, ExecutionStatus
from aetp_protocol.ids import MessageId, SessionId, TraceId, stable_id
from aetp_protocol.logs import LogLevel
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    CaseStatusEvent,
    ExecutionLogBatch,
    ExecutionLogEntry,
    ExecutionProgress,
    ExecutionReconcile,
    ExecutionReconcileResult,
    LogComplete,
    ReconcileAttempt,
)
from aetp_protocol.plan_hash import with_plan_hash
from aetp_protocol.topics import event_topic

from common.transport import MqttMessage
from master.application.services.plan_lease_service import PlanLeaseService
from master.application.services.plan_materialization_service import PlanMaterializationService
from master.domain.enums import DisconnectReason, ShardAttemptStatus, ShardStatus
from tests.test_m3_plan_lease import NOW, SESSION_ID, _plan
from tests.test_m3_plan_materialization import _seed_context

NEW_SESSION = SessionId("session-00000099")


def _message(plan, message_type: MessageType, payload, *, session_id: SessionId, suffix: str) -> MqttMessage:
    envelope = Envelope(
        message_id=MessageId(f"v2-event-message-{suffix}"),
        correlation_id=MessageId("v2-event-correlation-01"),
        sent_at=NOW + timedelta(seconds=1),
        sender=Sender(kind="agent", id=plan.node_id, session_id=session_id),
        message_type=message_type.value,
        trace_id=TraceId(f"v2-event-trace-{suffix}"),
        payload=payload.model_dump(mode="json"),
    )
    return MqttMessage(
        topic=event_topic(plan.node_id.root, {
            MessageType.EXECUTION_PROGRESS: "execution.progress",
            MessageType.EXECUTION_LOG: "execution.log",
            MessageType.EXECUTION_CASE_STATUS: "execution.case_status",
            MessageType.EXECUTION_LOG_COMPLETE: "execution.log_complete",
            MessageType.EXECUTION_RECONCILE: "execution.reconcile",
        }[message_type]),
        payload=envelope.model_dump_json().encode("utf-8"),
    )


def _new_session(container, plan) -> None:
    with container.uow_factory()() as uow:
        node = uow.nodes.get_by_id(plan.node_id.root)
        assert node is not None and node.id is not None
        current = uow.node_sessions.get_current(node.id)
        assert current is not None
        uow.node_sessions.close(current, reason=DisconnectReason.SESSION_REPLACED, at=NOW)
        uow.node_sessions.create(
            type(current)(
                node_pk=node.id,
                node_id=plan.node_id.root,
                session_id=NEW_SESSION.root,
                client_id="agent-new-session",
                connected_at=NOW + timedelta(seconds=1),
            )
        )


def _materialize(container, plan):
    _seed_context(container, plan)
    return PlanMaterializationService(
        container.uow_factory(),
        PlanLeaseService(container.uow_factory(), now=lambda: NOW),
    ).materialize(plan)


def test_master_runtime_events_are_sequence_safe_and_fenced(client) -> None:
    container = client.app.state.container
    plan = with_plan_hash(_plan().model_copy(update={"node_id": stable_id("m3-events-node")}))
    _materialize(container, plan)
    router = container.message_router()

    progress = ExecutionProgress(
        run_id=plan.run_id,
        shard_id=plan.shard_id,
        attempt_id=plan.attempt_id,
        plan_id=plan.plan_id,
        sequence=2,
        percent=20,
        stage="running",
    )
    assert asyncio.run(
        router.handle(
            _message(plan, MessageType.EXECUTION_PROGRESS, progress, session_id=SESSION_ID, suffix="p2")
        )
    )
    assert asyncio.run(
        router.handle(
            _message(plan, MessageType.EXECUTION_PROGRESS, progress, session_id=SESSION_ID, suffix="p2-retry")
        )
    )

    log = ExecutionLogBatch(
        run_id=plan.run_id,
        shard_id=plan.shard_id,
        attempt_id=plan.attempt_id,
        plan_id=plan.plan_id,
        first_sequence=1,
        entries=(
            ExecutionLogEntry(
                sequence=1,
                level=LogLevel.INFO,
                message="started",
                occurred_at=NOW,
            ),
        ),
    )
    assert asyncio.run(
        router.handle(_message(plan, MessageType.EXECUTION_LOG, log, session_id=SESSION_ID, suffix="log"))
    )
    assert asyncio.run(
        router.handle(_message(plan, MessageType.EXECUTION_LOG, log, session_id=SESSION_ID, suffix="log-retry"))
    )

    case_status = CaseStatusEvent(
        run_id=plan.run_id,
        shard_id=plan.shard_id,
        attempt_id=plan.attempt_id,
        plan_id=plan.plan_id,
        case_key=plan.case_keys[0],
        sequence=3,
        status=CaseStatus.RUNNING,
    )
    assert asyncio.run(
        router.handle(
            _message(plan, MessageType.EXECUTION_CASE_STATUS, case_status, session_id=SESSION_ID, suffix="case")
        )
    )
    complete = LogComplete(
        run_id=plan.run_id,
        shard_id=plan.shard_id,
        attempt_id=plan.attempt_id,
        plan_id=plan.plan_id,
        last_sequence=1,
        entry_count=1,
    )
    assert asyncio.run(
        router.handle(
            _message(plan, MessageType.EXECUTION_LOG_COMPLETE, complete, session_id=SESSION_ID, suffix="complete")
        )
    )
    assert (
        asyncio.run(
            router.handle(_message(plan, MessageType.EXECUTION_LOG, log, session_id=SESSION_ID, suffix="late"))
        )
        is False
    )

    with container.uow_factory()() as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(plan.attempt_id.root)
        logs = uow.run_logs.existing_attempt_sequences(plan.run_id.root, plan.attempt_id.root, [1])
        case = uow.run_case_results.get_by_key(plan.run_id.root, plan.shard_id.root, plan.case_keys[0], 1)
        assert attempt is not None and attempt.status is ShardAttemptStatus.RUNNING
        assert attempt.last_progress_sequence == 2
        assert attempt.log_complete is True
        assert logs == {1}
        assert case is not None and case.sequence == 3


def test_master_reconcile_accepts_new_session_and_projects_terminal_once(client) -> None:
    container = client.app.state.container
    plan = with_plan_hash(_plan().model_copy(update={"node_id": stable_id("m3-reconcile-node")}))
    _materialize(container, plan)
    with container.uow_factory()() as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(plan.attempt_id.root)
        shard = uow.run_shards.get_by_shard_id(plan.shard_id.root)
        assert attempt is not None and shard is not None
        attempt.status = ShardAttemptStatus.UNKNOWN
        attempt.finished_at = NOW
        shard.status = ShardStatus.WAITING_RECOVERY
        uow.shard_attempts.update(attempt)
        uow.run_shards.update(shard)
    _new_session(container, plan)

    report = ReconcileAttempt(
        attempt_id=plan.attempt_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        state="succeeded",
        last_progress_sequence=4,
        result=ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            passed=True,
            case_results=(CaseResult(case_key=plan.case_keys[0], status=CaseStatus.PASSED),),
        ),
    )
    reconcile = ExecutionReconcile(node_id=plan.node_id, attempts=(report,))
    router = container.message_router()
    message = _message(plan, MessageType.EXECUTION_RECONCILE, reconcile, session_id=NEW_SESSION, suffix="reconcile")
    assert asyncio.run(router.handle(message)) is True
    assert asyncio.run(router.handle(message)) is True

    with container.uow_factory()() as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(plan.attempt_id.root)
        shard = uow.run_shards.get_by_shard_id(plan.shard_id.root)
        run = uow.task_runs.get_by_run_id(plan.run_id.root)
        assert attempt is not None and attempt.status is ShardAttemptStatus.SUCCEEDED
        assert shard is not None and shard.status is ShardStatus.SUCCEEDED
        assert run is not None and run.status.value == "succeeded"
        result_message = uow.outbox_messages.get_by_outbox_id(
            stable_id("execution-reconcile-result:v2-event-message-reconcile").root
        )
        assert result_message is not None
        _envelope, result = parse_message(result_message.payload)
        assert isinstance(result, ExecutionReconcileResult)
        assert result.accepted is True
