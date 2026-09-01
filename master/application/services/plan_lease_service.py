"""M3 V2 ExecutionPlan 和 ResourceLease 应用服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from aetp_protocol.errors import ErrorCode
from aetp_protocol.execution import ExecutionPlan, LeaseState, ResourceLease
from aetp_protocol.ids import (
    BusinessId,
    MessageId,
    SessionId,
    TraceId,
    new_id,
    stable_id,
)
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import LeaseRenewed, LeaseRenewRequest
from aetp_protocol.plan_hash import calculate_plan_hash, canonical_plan_document, with_plan_hash
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender
from sqlalchemy.exc import IntegrityError

from master.domain.enums import OutboxStatus, ShardAttemptStatus, ShardStatus
from master.domain.models import ExecutionPlanRecord, OutboxMessage, ResourceLeaseRecord
from master.domain.repositories import UnitOfWork
from master.domain.state_machine import assert_transition
from master.domain.time import utcnow


class PlanRejected(ValueError):
    """Plan hash、节点 session 或 deadline 校验失败。"""


class ResourceLeaseConflict(ValueError):
    """资源已经被其它 active Lease 占用，或 Lease 发生并发冲突。"""


_RESOURCE_LEASE_EXPIRED = ErrorCode("RESOURCE_LEASE_EXPIRED")
_STALE_SESSION = ErrorCode("STALE_SESSION")
_STALE_ATTEMPT = ErrorCode("STALE_ATTEMPT")


class PlanLeaseService:
    """Master 权威的 V2 Plan/Lease 分配、续租和回收服务。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        master_id: str = "aetp-master",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._master_id = master_id
        self._now = now or utcnow

    def allocate(self, plan: ExecutionPlan) -> ExecutionPlanRecord:
        """原子申请 Plan 所需全部 Lease 并写入 execution.plan outbox。"""
        if calculate_plan_hash(plan) != plan.plan_hash:
            raise PlanRejected("ExecutionPlan plan_hash 校验失败")
        now = self._now()
        if plan.deadline_at <= now:
            raise PlanRejected("ExecutionPlan deadline 已过期")
        resource_ids = tuple(binding.resource_id.root for binding in plan.resource_bindings)
        if len(resource_ids) != len(set(resource_ids)):
            raise ResourceLeaseConflict("一个 Plan 不能重复申请同一 resource_id")
        if any(
            binding.expires_at <= now or binding.expires_at > plan.deadline_at
            for binding in plan.resource_bindings
        ):
            raise PlanRejected("Lease expires_at 必须晚于当前时间且不超过 Plan deadline")

        try:
            with self._uow_factory() as uow:
                existing = uow.execution_plans.get_by_plan_id(plan.plan_id)
                if existing is not None:
                    if existing.plan != plan:
                        raise PlanRejected("plan_id 已用于不同的 Plan")
                    return existing
                self._validate_target_session(uow, plan)
                for binding in plan.resource_bindings:
                    if uow.resource_leases.get_active_by_resource(binding.resource_id) is not None:
                        raise ResourceLeaseConflict(
                            f"资源已有 active Lease: {binding.resource_id.root}"
                        )
                    uow.resource_leases.add(
                        ResourceLeaseRecord(
                            id=None,
                            lease=ResourceLease(
                                lease_id=binding.lease_id,
                                run_id=plan.run_id,
                                shard_id=plan.shard_id,
                                attempt_id=plan.attempt_id,
                                node_id=plan.node_id,
                                resource_id=binding.resource_id,
                                state=LeaseState.ACTIVE,
                                revision=binding.lease_revision,
                                acquired_at=now,
                                heartbeat_at=now,
                                expires_at=binding.expires_at,
                            ),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                record = uow.execution_plans.add(
                    ExecutionPlanRecord(
                        id=None,
                        plan=plan,
                        created_at=plan.created_at,
                        updated_at=plan.created_at,
                    )
                )
                envelope = self._build_plan_envelope(plan)
                uow.outbox_messages.enqueue(
                    OutboxMessage(
                        outbox_id=stable_id(f"execution-plan:{plan.plan_id.root}").root,
                        aggregate_type="execution_plan",
                        aggregate_id=plan.plan_id.root,
                        topic=v2_command_topic(plan.node_id.root, "execution.plan"),
                        payload=envelope.model_dump(mode="json"),
                        qos=1,
                        status=OutboxStatus.PENDING,
                        attempts=0,
                        next_attempt_at=None,
                    )
                )
                return record
        except IntegrityError as exc:
            raise ResourceLeaseConflict("资源 Lease 发生数据库并发冲突") from exc

    def renew(
        self,
        request: LeaseRenewRequest,
        *,
        node_id: BusinessId,
        session_id: SessionId,
    ) -> LeaseRenewed:
        """按 lease_id + revision + 未过期条件续租。"""
        now = self._now()
        with self._uow_factory() as uow:
            record = uow.resource_leases.get_by_lease_id(request.lease_id)
            if record is None:
                return self._renewal_rejected(request, _RESOURCE_LEASE_EXPIRED)
            lease = record.lease
            if lease.node_id != node_id:
                return self._renewal_rejected(request, _STALE_SESSION, lease.revision)
            if lease.attempt_id != request.attempt_id:
                return self._renewal_rejected(request, _STALE_ATTEMPT, lease.revision)
            plan_record = uow.execution_plans.get_by_plan_id(request.plan_id)
            if plan_record is None or plan_record.plan.attempt_id != request.attempt_id:
                return self._renewal_rejected(request, _STALE_ATTEMPT, lease.revision)
            try:
                self._validate_current_session(uow, node_id, session_id)
            except PlanRejected:
                return self._renewal_rejected(request, _STALE_SESSION, lease.revision)
            if plan_record.plan.deadline_at <= now or request.requested_expires_at > plan_record.plan.deadline_at:
                return self._renewal_rejected(request, _RESOURCE_LEASE_EXPIRED, lease.revision)
            updated = uow.resource_leases.renew(
                request.lease_id,
                expected_revision=request.revision,
                requested_expires_at=request.requested_expires_at,
                now=now,
            )
            if updated is None:
                return self._renewal_rejected(request, _RESOURCE_LEASE_EXPIRED, lease.revision)
            return LeaseRenewed(
                plan_id=request.plan_id,
                attempt_id=request.attempt_id,
                lease_id=request.lease_id,
                accepted=True,
                revision=updated.lease.revision,
                expires_at=updated.lease.expires_at,
            )

    def release_lease(
        self,
        lease_id: BusinessId,
        *,
        expected_revision: int | None = None,
    ) -> ResourceLeaseRecord | None:
        """幂等释放一个 Lease；可选 revision 防止旧 Attempt 覆盖。"""
        with self._uow_factory() as uow:
            return uow.resource_leases.release(
                lease_id,
                now=self._now(),
                expected_revision=expected_revision,
            )

    def release_attempt(self, attempt_id: BusinessId) -> tuple[ResourceLeaseRecord, ...]:
        """释放 Attempt 的全部 active Lease。"""
        released: list[ResourceLeaseRecord] = []
        with self._uow_factory() as uow:
            for record in uow.resource_leases.list_by_attempt(attempt_id):
                if record.lease.state is not LeaseState.ACTIVE:
                    continue
                updated = uow.resource_leases.release(
                    record.lease.lease_id,
                    now=self._now(),
                    expected_revision=record.lease.revision,
                )
                if updated is not None:
                    released.append(updated)
        return tuple(released)

    def expire_due(self) -> tuple[ResourceLeaseRecord, ...]:
        """条件回收到期 Lease，并把受影响 Attempt 标记为 unknown。"""
        now = self._now()
        with self._uow_factory() as uow:
            expired = tuple(uow.resource_leases.expire_due(now=now))
            affected: set[str] = set()
            for record in expired:
                attempt_id = record.lease.attempt_id.root
                if attempt_id in affected:
                    continue
                affected.add(attempt_id)
                attempt = uow.shard_attempts.get_by_attempt_id(attempt_id)
                if attempt is None:
                    continue
                if attempt.status in {
                    ShardAttemptStatus.CREATED,
                    ShardAttemptStatus.DISPATCHED,
                    ShardAttemptStatus.ACKED,
                    ShardAttemptStatus.RUNNING,
                }:
                    assert_transition(attempt.status, ShardAttemptStatus.UNKNOWN)
                    attempt.status = ShardAttemptStatus.UNKNOWN
                    attempt.error_code = "RESOURCE_LEASE_EXPIRED"
                    attempt.error_message = "执行所需资源 Lease 已过期"
                    attempt.finished_at = now
                    uow.shard_attempts.update(attempt)
                shard = uow.run_shards.get_by_shard_id(attempt.shard_id)
                if shard is not None and shard.status in {
                    ShardStatus.DISPATCHING,
                    ShardStatus.RUNNING,
                }:
                    assert_transition(shard.status, ShardStatus.WAITING_RECOVERY)
                    shard.status = ShardStatus.WAITING_RECOVERY
                    uow.run_shards.update(shard)
                for active in uow.resource_leases.list_by_attempt(record.lease.attempt_id):
                    if active.lease.state is not LeaseState.ACTIVE:
                        continue
                    uow.resource_leases.release(
                        active.lease.lease_id,
                        now=now,
                        expected_revision=active.lease.revision,
                    )
            return expired

    def _validate_target_session(self, uow: UnitOfWork, plan: ExecutionPlan) -> None:
        node = uow.nodes.get_by_id(plan.node_id.root)
        if node is None or not node.online or node.id is None:
            raise PlanRejected("Plan 目标节点不存在或离线")
        self._validate_current_session(uow, plan.node_id, plan.target_session_id)

    @staticmethod
    def _validate_current_session(uow: UnitOfWork, node_id: BusinessId, session_id: SessionId) -> None:
        node = uow.nodes.get_by_id(node_id.root)
        if node is None or node.id is None:
            raise PlanRejected("Plan 目标节点不存在")
        current = uow.node_sessions.get_current(node.id)
        if current is None or current.session_id != session_id.root:
            raise PlanRejected("Plan 目标 session 已失效")

    def _build_plan_envelope(self, plan: ExecutionPlan) -> V2Envelope:
        return V2Envelope(
            message_id=MessageId(new_id()),
            sent_at=self._now(),
            sender=V2Sender(
                kind="master",
                id=stable_id(self._master_id),
                session_id=SessionId(stable_id(f"{self._master_id}:session").root),
            ),
            message_type=MessageType.EXECUTION_PLAN.value,
            trace_id=TraceId(new_id()),
            payload=plan.model_dump(mode="json"),
        )

    @staticmethod
    def _renewal_rejected(
        request: LeaseRenewRequest,
        code: ErrorCode,
        revision: int | None = None,
    ) -> LeaseRenewed:
        return LeaseRenewed(
            plan_id=request.plan_id,
            attempt_id=request.attempt_id,
            lease_id=request.lease_id,
            accepted=False,
            revision=revision or request.revision,
            code=code,
        )


__all__ = [
    "PlanLeaseService",
    "PlanRejected",
    "ResourceLeaseConflict",
    "calculate_plan_hash",
    "canonical_plan_document",
    "with_plan_hash",
]
