"""Master V2 execution.ack 和 lease.renew 消息服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionAck, LeaseRenewed, LeaseRenewRequest
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from master.application.services.plan_lease_service import PlanLeaseService
from master.domain.enums import OutboxStatus, RunStatus, ShardAttemptStatus
from master.domain.models import OutboxMessage
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

    def _is_current_session(self, node_id: BusinessId, session_id: SessionId) -> bool:
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(node_id.root)
            if node is None or node.id is None:
                return False
            current = uow.node_sessions.get_current(node.id)
            return current is not None and current.session_id == session_id.root


__all__ = ["V2ExecutionService"]
