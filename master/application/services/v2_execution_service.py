"""Master V2 execution.ack 和 lease.renew 消息服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from aetp_protocol.errors import ErrorCode
from aetp_protocol.execution import CancelRequest, ExecutionStatus
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    ExecutionAck,
    ExecutionCancel,
    ExecutionFinished,
    LeaseRenewed,
    LeaseRenewRequest,
)
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from master.application.services.plan_lease_service import PlanLeaseService
from master.domain.enums import CaseStatus, OutboxStatus, RunStatus, ShardAttemptStatus, ShardStatus
from master.domain.models import OutboxMessage, RunCaseResult
from master.domain.repositories import UnitOfWork
from master.domain.state_machine import assert_transition
from master.domain.time import utcnow


class V2ExecutionService:
    """投影 V2 Plan ACK，并处理 Agent 的 Lease 续租请求。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        plan_leases: PlanLeaseService,
        *,
        master_id: str = "aetp-master",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._plan_leases = plan_leases
        self._master_id = master_id
        self._now = now or utcnow

    def handle_execution_ack(
        self,
        ack: ExecutionAck,
        *,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """校验并投影 execution.ack；重复 ACK 幂等。"""
        with self._uow_factory() as uow:
            record = uow.execution_plans.get_by_plan_id(ack.plan_id)
            if record is None:
                return False
            plan = record.plan
            if (
                ack.plan_hash != plan.plan_hash
                or ack.run_id != plan.run_id
                or ack.shard_id != plan.shard_id
                or ack.attempt_id != plan.attempt_id
                or plan.node_id != sender_node_id
                or plan.target_session_id != sender_session_id
            ):
                return False
            attempt = uow.shard_attempts.get_by_attempt_id(ack.attempt_id.root)
            if attempt is not None and (
                attempt.node_id != sender_node_id.root
                or attempt.shard_id != ack.shard_id.root
            ):
                return False
            if not ack.accepted:
                self._project_rejected_ack(uow, ack, attempt)
                return True
            if attempt is None:
                return True
            if attempt.status is ShardAttemptStatus.DISPATCHED:
                assert_transition(attempt.status, ShardAttemptStatus.ACKED)
                attempt.status = ShardAttemptStatus.ACKED
                uow.shard_attempts.update(attempt)
            elif attempt.status not in {
                ShardAttemptStatus.ACKED,
                ShardAttemptStatus.RUNNING,
                ShardAttemptStatus.SUCCEEDED,
                ShardAttemptStatus.FAILED,
                ShardAttemptStatus.CANCELLED,
                ShardAttemptStatus.TIMED_OUT,
            }:
                return False
            run = uow.task_runs.get_by_run_id(ack.run_id.root)
            if run is not None and run.status is RunStatus.DISPATCHED:
                assert_transition(run.status, RunStatus.ACKED)
                run.status = RunStatus.ACKED
                uow.task_runs.update(run)
            return True

    def request_cancel(self, plan_id: BusinessId, *, reason: str = "") -> OutboxMessage:
        """为已持久化 Plan 创建幂等 execution.cancel command。"""
        with self._uow_factory() as uow:
            plan_record = uow.execution_plans.get_by_plan_id(plan_id)
            if plan_record is None:
                raise KeyError(f"ExecutionPlan 不存在: {plan_id.root}")
            plan = plan_record.plan
            node = uow.nodes.get_by_id(plan.node_id.root)
            if node is None or node.id is None:
                raise ValueError("Plan 目标节点不存在")
            session = uow.node_sessions.get_current(node.id)
            if session is None or session.session_id != plan.target_session_id.root:
                raise ValueError("Plan 目标 session 已失效")
            cancel = ExecutionCancel(
                request=CancelRequest(
                    cancel_request_id=MessageId(new_id()),
                    run_id=plan.run_id,
                    shard_id=plan.shard_id,
                    attempt_no=plan.attempt_no,
                    plan_id=plan.plan_id,
                    reason=reason,
                )
            )
            outbox_id = stable_id(f"execution-cancel:{cancel.request.cancel_request_id.root}").root
            envelope = V2Envelope(
                message_id=MessageId(new_id()),
                sent_at=self._now(),
                sender=V2Sender(
                    kind="master",
                    id=stable_id(self._master_id),
                    session_id=SessionId(stable_id(f"{self._master_id}:session").root),
                ),
                message_type=MessageType.EXECUTION_CANCEL.value,
                trace_id=TraceId(new_id()),
                payload=cancel.model_dump(mode="json"),
            )
            existing = uow.outbox_messages.get_by_outbox_id(outbox_id)
            if existing is not None:
                return existing
            return uow.outbox_messages.enqueue(
                OutboxMessage(
                    outbox_id=outbox_id,
                    aggregate_type="execution_plan",
                    aggregate_id=plan.plan_id.root,
                    topic=v2_command_topic(plan.node_id.root, "execution.cancel"),
                    payload=envelope.model_dump(mode="json"),
                    qos=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=None,
                )
            )

    def handle_lease_renew(
        self,
        request: LeaseRenewRequest,
        *,
        message_id: MessageId,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """处理 lease.renew，并把 LeaseRenewed 写入 V2 command outbox。"""
        outbox_id = stable_id(f"lease-renewed:{message_id.root}").root
        if not self._is_current_session(sender_node_id, sender_session_id):
            response = LeaseRenewed(
                plan_id=request.plan_id,
                attempt_id=request.attempt_id,
                lease_id=request.lease_id,
                accepted=False,
                revision=request.revision,
                code=ErrorCode("STALE_SESSION"),
            )
        else:
            with self._uow_factory() as uow:
                if uow.outbox_messages.get_by_outbox_id(outbox_id) is not None:
                    return True
            response = self._plan_leases.renew(
                request,
                node_id=sender_node_id,
                session_id=sender_session_id,
            )
        envelope = V2Envelope(
            message_id=MessageId(new_id()),
            correlation_id=message_id,
            sent_at=self._now(),
            sender=V2Sender(
                kind="master",
                id=stable_id(self._master_id),
                session_id=SessionId(stable_id(f"{self._master_id}:session").root),
            ),
            message_type=MessageType.LEASE_RENEWED.value,
            trace_id=TraceId(new_id()),
            payload=response.model_dump(mode="json"),
        )
        with self._uow_factory() as uow:
            if uow.outbox_messages.get_by_outbox_id(outbox_id) is None:
                uow.outbox_messages.enqueue(
                    OutboxMessage(
                        outbox_id=outbox_id,
                        aggregate_type="resource_lease",
                        aggregate_id=request.lease_id.root,
                        topic=v2_command_topic(sender_node_id.root, "lease.renewed"),
                        payload=envelope.model_dump(mode="json"),
                        qos=1,
                        status=OutboxStatus.PENDING,
                        attempts=0,
                        next_attempt_at=None,
                    )
                )
        return True

    def handle_execution_finished(
        self,
        finished: ExecutionFinished,
        *,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """校验并投影 execution.finished，同时释放 Attempt 的全部 Lease。"""
        with self._uow_factory() as uow:
            plan_record = uow.execution_plans.get_by_plan_id(finished.plan_id)
            if plan_record is None:
                return False
            plan = plan_record.plan
            if not self._matches_plan_identity(
                plan,
                run_id=finished.run_id,
                shard_id=finished.shard_id,
                attempt_id=finished.attempt_id,
                plan_hash=finished.plan_hash,
                sender_node_id=sender_node_id,
                sender_session_id=sender_session_id,
            ):
                return False
            attempt = uow.shard_attempts.get_by_attempt_id(finished.attempt_id.root)
            if attempt is None:
                return False
            terminal_attempts = {
                ShardAttemptStatus.SUCCEEDED,
                ShardAttemptStatus.FAILED,
                ShardAttemptStatus.CANCELLED,
                ShardAttemptStatus.TIMED_OUT,
                ShardAttemptStatus.LOST,
            }
            if attempt.status in terminal_attempts:
                return True
            target_attempt = {
                ExecutionStatus.SUCCEEDED: ShardAttemptStatus.SUCCEEDED,
                ExecutionStatus.FAILED: ShardAttemptStatus.FAILED,
                ExecutionStatus.CANCELLED: ShardAttemptStatus.CANCELLED,
                ExecutionStatus.TIMED_OUT: ShardAttemptStatus.TIMED_OUT,
            }[finished.result.status]
            if attempt.status is ShardAttemptStatus.DISPATCHED:
                assert_transition(attempt.status, ShardAttemptStatus.ACKED)
                attempt.status = ShardAttemptStatus.ACKED
            if attempt.status is ShardAttemptStatus.ACKED:
                assert_transition(attempt.status, ShardAttemptStatus.RUNNING)
                attempt.status = ShardAttemptStatus.RUNNING
            assert_transition(attempt.status, target_attempt)
            attempt.status = target_attempt
            if finished.result.error is not None:
                attempt.error_code = finished.result.error.code.root
                attempt.error_message = finished.result.error.message
            attempt.finished_at = finished.finished_at
            uow.shard_attempts.update(attempt)
            if finished.result.case_results:
                uow.run_case_results.add_many(
                    [
                        RunCaseResult(
                            run_id=finished.run_id.root,
                            shard_id=finished.shard_id.root,
                            case_key=case.case_key,
                            attempt_no=attempt.attempt_no,
                            status=CaseStatus(case.status.value),
                            duration_ms=case.duration_ms,
                            error_summary=case.error_summary,
                            detail=case.detail,
                        )
                        for case in finished.result.case_results
                    ]
                )

            shard = uow.run_shards.get_by_shard_id(finished.shard_id.root)
            if shard is not None and shard.status not in {
                ShardStatus.SUCCEEDED,
                ShardStatus.FAILED,
                ShardStatus.CANCELLED,
                ShardStatus.TIMED_OUT,
            }:
                target_shard = {
                    ExecutionStatus.SUCCEEDED: ShardStatus.SUCCEEDED,
                    ExecutionStatus.FAILED: ShardStatus.FAILED,
                    ExecutionStatus.CANCELLED: ShardStatus.CANCELLED,
                    ExecutionStatus.TIMED_OUT: ShardStatus.TIMED_OUT,
                }[finished.result.status]
                if shard.status is ShardStatus.DISPATCHING:
                    assert_transition(shard.status, ShardStatus.RUNNING)
                    shard.status = ShardStatus.RUNNING
                assert_transition(shard.status, target_shard)
                shard.status = target_shard
                shard.final_node = sender_node_id.root
                uow.run_shards.update(shard)

            for lease_record in uow.resource_leases.list_by_attempt(finished.attempt_id):
                if lease_record.lease.state.value != "active":
                    continue
                uow.resource_leases.release(
                    lease_record.lease.lease_id,
                    now=self._now(),
                    expected_revision=lease_record.lease.revision,
                )
            self._project_run_if_terminal(uow, finished.run_id)
            return True

    def _project_rejected_ack(self, uow: UnitOfWork, ack: ExecutionAck, attempt) -> None:
        if attempt is not None and attempt.status not in {
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.CANCELLED,
            ShardAttemptStatus.SUCCEEDED,
            ShardAttemptStatus.TIMED_OUT,
        }:
            assert_transition(attempt.status, ShardAttemptStatus.FAILED)
            attempt.status = ShardAttemptStatus.FAILED
            attempt.error_code = ack.code.root if ack.code is not None else "EXECUTION_PLAN_INVALID"
            attempt.error_message = ack.message
            attempt.finished_at = self._now()
            uow.shard_attempts.update(attempt)
        for record in uow.resource_leases.list_by_attempt(ack.attempt_id):
            if record.lease.state.value != "active":
                continue
            uow.resource_leases.release(
                record.lease.lease_id,
                now=self._now(),
                expected_revision=record.lease.revision,
            )

    @staticmethod
    def _matches_plan_identity(
        plan,
        *,
        run_id: BusinessId,
        shard_id: BusinessId,
        attempt_id: BusinessId,
        plan_hash,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        return (
            plan.plan_hash == plan_hash
            and plan.run_id == run_id
            and plan.shard_id == shard_id
            and plan.attempt_id == attempt_id
            and plan.node_id == sender_node_id
            and plan.target_session_id == sender_session_id
        )

    @staticmethod
    def _project_run_if_terminal(uow: UnitOfWork, run_id: BusinessId) -> None:
        run = uow.task_runs.get_by_run_id(run_id.root)
        if run is None or run.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.LOST,
        }:
            return
        shards = uow.run_shards.list_by_run(run_id.root)
        terminal = {
            ShardStatus.SUCCEEDED,
            ShardStatus.FAILED,
            ShardStatus.CANCELLED,
            ShardStatus.TIMED_OUT,
        }
        if not shards or not all(shard.status in terminal for shard in shards):
            return
        target = RunStatus.SUCCEEDED
        if any(shard.status in {ShardStatus.FAILED, ShardStatus.TIMED_OUT} for shard in shards):
            target = RunStatus.FAILED
        elif any(shard.status is ShardStatus.CANCELLED for shard in shards):
            target = RunStatus.CANCELLED
        if run.status in {RunStatus.DISPATCHED, RunStatus.ACKED}:
            assert_transition(run.status, RunStatus.RUNNING)
            run.status = RunStatus.RUNNING
        assert_transition(run.status, target)
        run.status = target
        run.finished_at = utcnow()
        uow.task_runs.update(run)
    def _is_current_session(self, node_id: BusinessId, session_id: SessionId) -> bool:
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(node_id.root)
            if node is None or node.id is None:
                return False
            current = uow.node_sessions.get_current(node.id)
            return current is not None and current.session_id == session_id.root


__all__ = ["V2ExecutionService"]
