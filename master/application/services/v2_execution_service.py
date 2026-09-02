"""Master V2 execution.ack 和 lease.renew 消息服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from aetp_protocol.errors import ErrorCode
from aetp_protocol.execution import CancelRequest, ExecutionStatus
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    CaseStatusEvent,
    ExecutionAck,
    ExecutionCancel,
    ExecutionFinished,
    ExecutionLogBatch,
    ExecutionProgress,
    ExecutionReconcile,
    ExecutionReconcileResult,
    LeaseRenewed,
    LeaseRenewRequest,
    LogComplete,
)
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from master.application.services.plan_lease_service import PlanLeaseService
from master.domain.enums import CaseStatus, OutboxStatus, RunLogLevel, RunStatus, ShardAttemptStatus, ShardStatus
from master.domain.models import OutboxMessage, RunCaseResult, RunLog, RunResult
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

    def handle_execution_progress(
        self,
        progress: ExecutionProgress,
        *,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """投影 V2 progress；低于已确认序号的消息幂等丢弃。"""
        with self._uow_factory() as uow:
            plan_record = uow.execution_plans.get_by_plan_id(progress.plan_id)
            if plan_record is None or not self._matches_runtime_identity(
                plan_record.plan,
                run_id=progress.run_id,
                shard_id=progress.shard_id,
                attempt_id=progress.attempt_id,
                sender_node_id=sender_node_id,
                sender_session_id=sender_session_id,
            ):
                return False
            attempt = uow.shard_attempts.get_by_attempt_id(progress.attempt_id.root)
            if attempt is None or attempt.shard_id != progress.shard_id.root:
                return False
            if self._is_terminal_attempt(attempt.status):
                return True
            if attempt.status is ShardAttemptStatus.UNKNOWN:
                return False
            if progress.sequence <= attempt.last_progress_sequence:
                return True
            self._mark_attempt_running(uow, attempt)
            attempt.last_progress_sequence = progress.sequence
            uow.shard_attempts.update(attempt)
            shard = uow.run_shards.get_by_shard_id(progress.shard_id.root)
            if shard is not None and shard.status is ShardStatus.DISPATCHING:
                assert_transition(shard.status, ShardStatus.RUNNING)
                shard.status = ShardStatus.RUNNING
                uow.run_shards.update(shard)
            run = uow.task_runs.get_by_run_id(progress.run_id.root)
            if run is not None and run.status in {RunStatus.CREATED, RunStatus.DISPATCHED, RunStatus.ACKED}:
                if run.status is RunStatus.CREATED:
                    assert_transition(run.status, RunStatus.DISPATCHED)
                    run.status = RunStatus.DISPATCHED
                assert_transition(run.status, RunStatus.RUNNING)
                run.status = RunStatus.RUNNING
                run.started_at = run.started_at or self._now()
                uow.task_runs.update(run)
            return True

    def handle_execution_log(
        self,
        batch: ExecutionLogBatch,
        *,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """投影 V2 execution.log，按 Attempt 和 sequence 幂等。"""
        with self._uow_factory() as uow:
            plan_record = uow.execution_plans.get_by_plan_id(batch.plan_id)
            if plan_record is None or not self._matches_runtime_identity(
                plan_record.plan,
                run_id=batch.run_id,
                shard_id=batch.shard_id,
                attempt_id=batch.attempt_id,
                sender_node_id=sender_node_id,
                sender_session_id=sender_session_id,
            ):
                return False
            attempt = uow.shard_attempts.get_by_attempt_id(batch.attempt_id.root)
            if attempt is None or attempt.shard_id != batch.shard_id.root or attempt.log_complete:
                return False
            sequences = [entry.sequence for entry in batch.entries]
            existing = uow.run_logs.existing_attempt_sequences(
                batch.run_id.root,
                batch.attempt_id.root,
                sequences,
            )
            entries = [
                entry
                for entry in batch.entries
                if entry.sequence not in existing and entry.sequence > attempt.last_log_sequence
            ]
            if not entries:
                return True
            uow.run_logs.add_many(
                [
                    RunLog(
                        run_id=batch.run_id.root,
                        shard_id=batch.shard_id.root,
                        node_id=sender_node_id.root,
                        attempt_id=batch.attempt_id.root,
                        plan_id=batch.plan_id.root,
                        sequence=entry.sequence,
                        level=RunLogLevel(entry.level.value),
                        message=entry.message,
                        detail=dict(entry.detail),
                        occurred_at=entry.occurred_at,
                    )
                    for entry in entries
                ]
            )
            attempt.last_log_sequence = max(attempt.last_log_sequence, max(sequences))
            uow.shard_attempts.update(attempt)
            return True

    def handle_execution_case_status(
        self,
        event: CaseStatusEvent,
        *,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """投影 V2 case status，按 case sequence 防止旧状态覆盖新状态。"""
        with self._uow_factory() as uow:
            plan_record = uow.execution_plans.get_by_plan_id(event.plan_id)
            if plan_record is None or not self._matches_runtime_identity(
                plan_record.plan,
                run_id=event.run_id,
                shard_id=event.shard_id,
                attempt_id=event.attempt_id,
                sender_node_id=sender_node_id,
                sender_session_id=sender_session_id,
            ):
                return False
            if event.case_key not in plan_record.plan.case_keys:
                return False
            attempt = uow.shard_attempts.get_by_attempt_id(event.attempt_id.root)
            if attempt is None or attempt.shard_id != event.shard_id.root:
                return False
            existing = uow.run_case_results.get_by_key(
                event.run_id.root,
                event.shard_id.root,
                event.case_key,
                attempt.attempt_no,
            )
            if existing is not None and event.sequence <= existing.sequence:
                return True
            target_status = CaseStatus(event.status.value)
            if existing is not None:
                if existing.status in {
                    CaseStatus.PASSED,
                    CaseStatus.FAILED,
                    CaseStatus.SKIPPED,
                    CaseStatus.ERROR,
                } and target_status in {CaseStatus.PENDING, CaseStatus.RUNNING}:
                    return False
                existing.status = target_status
                existing.sequence = event.sequence
                uow.run_case_results.update(existing)
            else:
                uow.run_case_results.add_many(
                    [
                        RunCaseResult(
                            run_id=event.run_id.root,
                            shard_id=event.shard_id.root,
                            case_key=event.case_key,
                            attempt_no=attempt.attempt_no,
                            status=target_status,
                            sequence=event.sequence,
                        )
                    ]
                )
            return True

    def handle_execution_log_complete(
        self,
        complete: LogComplete,
        *,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """关闭 V2 Attempt 日志围栏；相同声明幂等，倒退声明拒绝。"""
        with self._uow_factory() as uow:
            plan_record = uow.execution_plans.get_by_plan_id(complete.plan_id)
            if plan_record is None or not self._matches_runtime_identity(
                plan_record.plan,
                run_id=complete.run_id,
                shard_id=complete.shard_id,
                attempt_id=complete.attempt_id,
                sender_node_id=sender_node_id,
                sender_session_id=sender_session_id,
            ):
                return False
            attempt = uow.shard_attempts.get_by_attempt_id(complete.attempt_id.root)
            if attempt is None or attempt.shard_id != complete.shard_id.root:
                return False
            if attempt.log_complete:
                return (
                    attempt.last_log_sequence == complete.last_sequence
                    and attempt.log_entry_count == complete.entry_count
                )
            if complete.last_sequence < attempt.last_log_sequence:
                return False
            attempt.log_complete = True
            attempt.last_log_sequence = complete.last_sequence
            attempt.log_entry_count = complete.entry_count
            uow.shard_attempts.update(attempt)
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
        allow_reconciled_session: bool = False,
    ) -> bool:
        """校验并投影 execution.finished，同时释放 Attempt 的全部 Lease。"""
        with self._uow_factory() as uow:
            plan_record = uow.execution_plans.get_by_plan_id(finished.plan_id)
            if plan_record is None:
                return False
            plan = plan_record.plan
            matches = (
                self._matches_reconciled_identity(
                    plan,
                    run_id=finished.run_id,
                    shard_id=finished.shard_id,
                    attempt_id=finished.attempt_id,
                    sender_node_id=sender_node_id,
                )
                if allow_reconciled_session
                else self._matches_plan_identity(
                    plan,
                    run_id=finished.run_id,
                    shard_id=finished.shard_id,
                    attempt_id=finished.attempt_id,
                    plan_hash=finished.plan_hash,
                    sender_node_id=sender_node_id,
                    sender_session_id=sender_session_id,
                )
            )
            if not matches or plan.plan_hash != finished.plan_hash:
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
            for artifact in finished.result.artifacts:
                stored_artifact = uow.run_artifacts.get_by_artifact_id(artifact.artifact_id.root)
                if (
                    stored_artifact is None
                    or stored_artifact.run_id != finished.run_id.root
                    or stored_artifact.shard_id != finished.shard_id.root
                    or stored_artifact.attempt_id != finished.attempt_id.root
                    or artifact.project_id != plan.project_id
                    or artifact.node_id != plan.node_id
                    or artifact.kind.value != stored_artifact.kind.value
                    or stored_artifact.size != artifact.size
                    or stored_artifact.sha256 != artifact.sha256.root
                ):
                    return False
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
            self._project_v2_run_result(uow, finished, sender_node_id)
            return True

    def handle_execution_reconcile(
        self,
        reconcile: ExecutionReconcile,
        *,
        message_id: MessageId,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """处理 Agent 重连对账并发布 execution.reconcile_result。"""
        response_outbox_id = stable_id(f"execution-reconcile-result:{message_id.root}").root
        with self._uow_factory() as uow:
            if uow.outbox_messages.get_by_outbox_id(response_outbox_id) is not None:
                return True
            current = uow.nodes.get_by_id(sender_node_id.root)
            current_session = (
                uow.node_sessions.get_current(current.id)
                if current is not None and current.id is not None
                else None
            )
            current_session_matches = (
                current_session is not None
                and current_session.session_id == sender_session_id.root
            )
        if reconcile.node_id != sender_node_id or not current_session_matches:
            response = ExecutionReconcileResult(
                node_id=sender_node_id,
                accepted=False,
                code=ErrorCode("STALE_SESSION"),
                message="对账来自非当前 Agent session",
            )
            self._enqueue_reconcile_result(response_outbox_id, message_id, sender_node_id, response)
            return True

        for reported in reconcile.attempts:
            validation = self._validate_reconcile_attempt(
                reported,
                sender_node_id=sender_node_id,
            )
            if validation is not None:
                response = ExecutionReconcileResult(
                    node_id=sender_node_id,
                    accepted=False,
                    code=validation[0],
                    message=validation[1],
                )
                self._enqueue_reconcile_result(response_outbox_id, message_id, sender_node_id, response)
                return True
            if reported.state == "running":
                self._recover_running_attempt(reported, sender_node_id)
                continue
            assert reported.result is not None
            finished = ExecutionFinished(
                run_id=self._plan_identities(reported)[0],
                shard_id=self._plan_identities(reported)[1],
                attempt_id=reported.attempt_id,
                plan_id=reported.plan_id,
                plan_hash=reported.plan_hash,
                result=reported.result,
                finished_at=self._now(),
            )
            if not self.handle_execution_finished(
                finished,
                sender_node_id=sender_node_id,
                sender_session_id=sender_session_id,
                allow_reconciled_session=True,
            ):
                response = ExecutionReconcileResult(
                    node_id=sender_node_id,
                    accepted=False,
                    code=ErrorCode("STALE_ATTEMPT"),
                    message="对账终态无法投影到当前 Attempt",
                )
                self._enqueue_reconcile_result(response_outbox_id, message_id, sender_node_id, response)
                return True

        response = ExecutionReconcileResult(
            node_id=sender_node_id,
            accepted=True,
            attempts=reconcile.attempts,
        )
        self._enqueue_reconcile_result(response_outbox_id, message_id, sender_node_id, response)
        return True

    def _validate_reconcile_attempt(
        self,
        reported,
        *,
        sender_node_id: BusinessId,
    ) -> tuple[ErrorCode, str] | None:
        with self._uow_factory() as uow:
            plan_record = uow.execution_plans.get_by_plan_id(reported.plan_id)
            if plan_record is None:
                return ErrorCode("STALE_ATTEMPT"), "对账 Plan 不存在"
            plan = plan_record.plan
            if (
                plan.node_id != sender_node_id
                or plan.attempt_id != reported.attempt_id
                or plan.plan_hash != reported.plan_hash
            ):
                return ErrorCode("STALE_ATTEMPT"), "对账 Plan 身份不一致"
            attempt = uow.shard_attempts.get_by_attempt_id(reported.attempt_id.root)
            if attempt is None or attempt.node_id != sender_node_id.root:
                return ErrorCode("STALE_ATTEMPT"), "对账 Attempt 不存在或节点不一致"
            if reported.state == "running":
                if self._is_terminal_attempt(attempt.status):
                    return ErrorCode("STALE_ATTEMPT"), "已终态 Attempt 不能恢复运行"
                return None
            expected = {
                "succeeded": ExecutionStatus.SUCCEEDED,
                "failed": ExecutionStatus.FAILED,
                "cancelled": ExecutionStatus.CANCELLED,
                "timed_out": ExecutionStatus.TIMED_OUT,
            }[reported.state]
            if reported.result is None or reported.result.status is not expected:
                return ErrorCode("EXECUTION_FAILED"), "对账终态缺少匹配的执行结果"
            return None

    def _recover_running_attempt(self, reported, sender_node_id: BusinessId) -> None:
        with self._uow_factory() as uow:
            attempt = uow.shard_attempts.get_by_attempt_id(reported.attempt_id.root)
            if attempt is None:
                return
            if attempt.status in {ShardAttemptStatus.DISPATCHED, ShardAttemptStatus.ACKED}:
                self._mark_attempt_running(uow, attempt)
            elif attempt.status is ShardAttemptStatus.UNKNOWN:
                assert_transition(attempt.status, ShardAttemptStatus.RUNNING)
                attempt.status = ShardAttemptStatus.RUNNING
                attempt.started_at = attempt.started_at or self._now()
            elif attempt.status is not ShardAttemptStatus.RUNNING:
                return
            attempt.last_progress_sequence = max(
                attempt.last_progress_sequence,
                reported.last_progress_sequence,
            )
            uow.shard_attempts.update(attempt)
            shard = uow.run_shards.get_by_shard_id(attempt.shard_id)
            if shard is not None and shard.status in {ShardStatus.DISPATCHING, ShardStatus.WAITING_RECOVERY}:
                assert_transition(shard.status, ShardStatus.RUNNING)
                shard.status = ShardStatus.RUNNING
                shard.final_node = sender_node_id.root
                uow.run_shards.update(shard)
            plan_record = uow.execution_plans.get_by_plan_id(reported.plan_id)
            run = (
                uow.task_runs.get_by_run_id(plan_record.plan.run_id.root)
                if plan_record is not None
                else None
            )
            if run is not None and run.status in {RunStatus.CREATED, RunStatus.DISPATCHED, RunStatus.ACKED}:
                if run.status is RunStatus.CREATED:
                    assert_transition(run.status, RunStatus.DISPATCHED)
                    run.status = RunStatus.DISPATCHED
                assert_transition(run.status, RunStatus.RUNNING)
                run.status = RunStatus.RUNNING
                run.started_at = run.started_at or self._now()
                uow.task_runs.update(run)

    def _enqueue_reconcile_result(
        self,
        outbox_id: str,
        message_id: MessageId,
        node_id: BusinessId,
        result: ExecutionReconcileResult,
    ) -> None:
        envelope = V2Envelope(
            message_id=MessageId(new_id()),
            correlation_id=message_id,
            sent_at=self._now(),
            sender=V2Sender(
                kind="master",
                id=stable_id(self._master_id),
                session_id=SessionId(stable_id(f"{self._master_id}:session").root),
            ),
            message_type=MessageType.EXECUTION_RECONCILE_RESULT.value,
            trace_id=TraceId(new_id()),
            payload=result.model_dump(mode="json"),
        )
        with self._uow_factory() as uow:
            if uow.outbox_messages.get_by_outbox_id(outbox_id) is None:
                uow.outbox_messages.enqueue(
                    OutboxMessage(
                        outbox_id=outbox_id,
                        aggregate_type="execution_reconcile",
                        aggregate_id=node_id.root,
                        topic=v2_command_topic(node_id.root, "execution.reconcile_result"),
                        payload=envelope.model_dump(mode="json"),
                        qos=1,
                        status=OutboxStatus.PENDING,
                        attempts=0,
                        next_attempt_at=None,
                    )
                )

    def _plan_identities(self, reported):
        with self._uow_factory() as uow:
            record = uow.execution_plans.get_by_plan_id(reported.plan_id)
            if record is None:
                raise ValueError("对账 Plan 不存在")
            return record.plan.run_id, record.plan.shard_id

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
    def _matches_runtime_identity(
        plan,
        *,
        run_id: BusinessId,
        shard_id: BusinessId,
        attempt_id: BusinessId,
        sender_node_id: BusinessId,
        sender_session_id: SessionId,
    ) -> bool:
        """校验运行期事件的 Plan、节点和当前 session 身份。"""
        return (
            plan.run_id == run_id
            and plan.shard_id == shard_id
            and plan.attempt_id == attempt_id
            and plan.node_id == sender_node_id
            and plan.target_session_id == sender_session_id
        )

    @staticmethod
    def _matches_reconciled_identity(
        plan,
        *,
        run_id: BusinessId,
        shard_id: BusinessId,
        attempt_id: BusinessId,
        sender_node_id: BusinessId,
    ) -> bool:
        """校验重连对账身份；对账允许使用新 session。"""
        return (
            plan.run_id == run_id
            and plan.shard_id == shard_id
            and plan.attempt_id == attempt_id
            and plan.node_id == sender_node_id
        )

    @staticmethod
    def _is_terminal_attempt(status: ShardAttemptStatus) -> bool:
        return status in {
            ShardAttemptStatus.SUCCEEDED,
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.CANCELLED,
            ShardAttemptStatus.TIMED_OUT,
            ShardAttemptStatus.LOST,
        }

    @staticmethod
    def _mark_attempt_running(uow: UnitOfWork, attempt) -> None:
        if attempt.status is ShardAttemptStatus.DISPATCHED:
            assert_transition(attempt.status, ShardAttemptStatus.ACKED)
            attempt.status = ShardAttemptStatus.ACKED
        if attempt.status is ShardAttemptStatus.ACKED:
            assert_transition(attempt.status, ShardAttemptStatus.RUNNING)
            attempt.status = ShardAttemptStatus.RUNNING
            attempt.started_at = attempt.started_at or utcnow()

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

    def _project_v2_run_result(
        self,
        uow: UnitOfWork,
        finished: ExecutionFinished,
        sender_node_id: BusinessId,
    ) -> None:
        """将所有 V2 Shard 收敛后的结果写入统一 RunResult 投影。"""
        run = uow.task_runs.get_by_run_id(finished.run_id.root)
        if run is None or run.status not in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.LOST,
        }:
            return
        case_results = uow.run_case_results.list_by_run(run.run_id)
        metrics = {
            "total": len(case_results),
            "passed": sum(item.status is CaseStatus.PASSED for item in case_results),
            "failed": sum(item.status in {CaseStatus.FAILED, CaseStatus.ERROR} for item in case_results),
            "skipped": sum(item.status is CaseStatus.SKIPPED for item in case_results),
        }
        data = dict(finished.result.data)
        data["artifact_ids"] = [
            artifact.artifact_id
            for artifact in uow.run_artifacts.list_by_run(run.run_id)
        ]
        result = uow.run_results.get_by_run_id(run.run_id)
        if result is None:
            uow.run_results.add(
                RunResult(
                    result_id=new_id(),
                    run_id=run.run_id,
                    project_id=run.project_id,
                    task_id=run.task_id,
                    node_id=sender_node_id.root,
                    passed=run.status is RunStatus.SUCCEEDED,
                    status=run.status,
                    metrics=metrics,
                    data=data,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
            )
            return
        result.node_id = sender_node_id.root
        result.passed = run.status is RunStatus.SUCCEEDED
        result.status = run.status
        result.metrics = metrics
        result.data = data
        result.started_at = run.started_at
        result.finished_at = run.finished_at
        uow.run_results.update(result)
    def _is_current_session(self, node_id: BusinessId, session_id: SessionId) -> bool:
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(node_id.root)
            if node is None or node.id is None:
                return False
            current = uow.node_sessions.get_current(node.id)
            return current is not None and current.session_id == session_id.root


__all__ = ["V2ExecutionService"]
