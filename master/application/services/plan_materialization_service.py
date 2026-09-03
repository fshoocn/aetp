"""M3  Plan 物化服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aetp_protocol.execution import ExecutionPlan

from master.application.services.plan_lease_service import PlanLeaseService, PlanRejected
from master.domain.enums import RunStatus, ShardAttemptStatus, ShardStatus
from master.domain.models import ExecutionPlanRecord, ShardAttempt
from master.domain.repositories import UnitOfWork
from master.domain.state_machine import assert_transition


@dataclass(frozen=True)
class MaterializedPlan:
    """已原子物化的  Plan 和 Attempt。"""

    plan: ExecutionPlanRecord
    attempt: ShardAttempt


class PlanMaterializationService:
    """在已有 Run/Shard 上原子创建  Attempt 并委托 Lease/Plan 持久化。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        plan_leases: PlanLeaseService,
    ) -> None:
        self._uow_factory = uow_factory
        self._plan_leases = plan_leases

    def materialize(self, plan: ExecutionPlan) -> MaterializedPlan:
        """创建或幂等重放一个  Attempt/Plan。"""
        with self._uow_factory() as uow:
            existing_plan = uow.execution_plans.get_by_plan_id(plan.plan_id)
            if existing_plan is not None:
                if existing_plan.plan != plan:
                    raise PlanRejected("plan_id 已用于不同的 Plan")
                attempt = uow.shard_attempts.get_by_attempt_id(plan.attempt_id.root)
                if attempt is None:
                    raise PlanRejected("已存在 Plan 但缺少对应 Attempt")
                return MaterializedPlan(existing_plan, attempt)
            existing_attempt_plan = uow.execution_plans.get_by_attempt(
                plan.run_id,
                plan.script_binding_id,
                plan.shard_id,
                plan.attempt_no,
            )
            if existing_attempt_plan is not None:
                raise PlanRejected("同一 Run/脚本绑定/Shard/Attempt 已存在其它 Plan")

            run = uow.task_runs.get_by_run_id(plan.run_id.root)
            if run is None or run.task_id != plan.task_id.root or run.project_id != plan.project_id.root:
                raise PlanRejected("Plan 引用的 Run 不存在或范围不一致")
            shard = uow.run_shards.get_by_shard_id(plan.shard_id.root)
            if shard is None or shard.run_id != plan.run_id.root:
                raise PlanRejected("Plan 引用的 Shard 不存在或不属于 Run")
            attempt = uow.shard_attempts.get_by_attempt_id(plan.attempt_id.root)
            if attempt is not None:
                if (
                    attempt.shard_id != plan.shard_id.root
                    or attempt.attempt_no != plan.attempt_no
                    or attempt.node_id != plan.node_id.root
                ):
                    raise PlanRejected("Plan 与已有 Attempt 控制字段不一致")
                if attempt.status is not ShardAttemptStatus.DISPATCHED:
                    if attempt.status is not ShardAttemptStatus.CREATED:
                        raise PlanRejected("已有 Attempt 不允许再次物化 Plan")
                    assert_transition(attempt.status, ShardAttemptStatus.DISPATCHED)
                    attempt.status = ShardAttemptStatus.DISPATCHED
                    uow.shard_attempts.update(attempt)
            else:
                if shard.status not in {ShardStatus.PENDING, ShardStatus.WAITING_RECOVERY, ShardStatus.DISPATCHING}:
                    raise PlanRejected("Shard 当前状态不允许物化  Plan")
                existing_attempt = uow.shard_attempts.get_by_shard_attempt(
                    plan.shard_id.root,
                    plan.attempt_no,
                )
                if existing_attempt is not None:
                    raise PlanRejected("同一 Shard/Attempt 序号已存在其它 Attempt")
                previous_attempts = uow.shard_attempts.list_by_shard(plan.shard_id.root)
                if shard.status is ShardStatus.DISPATCHING and any(
                    attempt.status
                    not in {
                        ShardAttemptStatus.SUCCEEDED,
                        ShardAttemptStatus.FAILED,
                        ShardAttemptStatus.CANCELLED,
                        ShardAttemptStatus.TIMED_OUT,
                        ShardAttemptStatus.LOST,
                    }
                    for attempt in previous_attempts
                ):
                    raise PlanRejected("Shard 仍有未终态 Attempt")
                attempt = uow.shard_attempts.add(
                    ShardAttempt(
                        attempt_id=plan.attempt_id.root,
                        shard_id=plan.shard_id.root,
                        attempt_no=plan.attempt_no,
                        node_id=plan.node_id.root,
                        device_ids=[binding.resource_id.root for binding in plan.resource_bindings],
                        status=ShardAttemptStatus.DISPATCHED,
                    )
                )

            if shard.status is not ShardStatus.DISPATCHING:
                assert_transition(shard.status, ShardStatus.DISPATCHING)
                shard.status = ShardStatus.DISPATCHING
                uow.run_shards.update(shard)
            if run.status is RunStatus.CREATED:
                assert_transition(run.status, RunStatus.DISPATCHED)
                run.status = RunStatus.DISPATCHED
                uow.task_runs.update(run)
            record = self._plan_leases.allocate_in_uow(uow, plan)
            return MaterializedPlan(record, attempt)


__all__ = ["MaterializedPlan", "PlanMaterializationService"]
