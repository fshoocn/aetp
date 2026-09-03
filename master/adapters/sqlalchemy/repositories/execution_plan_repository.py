""" ExecutionPlan 仓储实现。"""

from __future__ import annotations

from aetp_protocol.execution import ExecutionPlan
from aetp_protocol.ids import BusinessId
from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import ExecutionPlan as PlanORM
from master.domain.models import ExecutionPlanRecord
from master.domain.repositories import ExecutionPlanRepository


def _to_domain(orm: PlanORM) -> ExecutionPlanRecord:
    plan = ExecutionPlan.model_validate(orm.snapshot)
    if (
        orm.plan_id != plan.plan_id.root
        or orm.run_id != plan.run_id.root
        or orm.script_binding_id != plan.script_binding_id.root
        or orm.shard_id != plan.shard_id.root
        or orm.attempt_no != plan.attempt_no
        or orm.plan_hash != plan.plan_hash.root
    ):
        raise ValueError("ExecutionPlan 控制字段与快照不一致")
    return ExecutionPlanRecord(
        id=orm.id,
        plan=plan,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class ExecutionPlanRepositoryImpl(ExecutionPlanRepository):
    """保存不可变 Plan 快照并提供业务唯一键查询。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_plan_id(self, plan_id: BusinessId) -> ExecutionPlanRecord | None:
        orm = self._s.execute(
            select(PlanORM).where(PlanORM.plan_id == plan_id.root)
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def get_by_attempt(
        self,
        run_id: BusinessId,
        script_binding_id: BusinessId,
        shard_id: BusinessId,
        attempt_no: int,
    ) -> ExecutionPlanRecord | None:
        orm = self._s.execute(
            select(PlanORM).where(
                PlanORM.run_id == run_id.root,
                PlanORM.script_binding_id == script_binding_id.root,
                PlanORM.shard_id == shard_id.root,
                PlanORM.attempt_no == attempt_no,
            )
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list_by_run(self, run_id: BusinessId) -> list[ExecutionPlanRecord]:
        statement = (
            select(PlanORM)
            .where(PlanORM.run_id == run_id.root)
            .order_by(PlanORM.created_at, PlanORM.id)
        )
        return [_to_domain(item) for item in self._s.execute(statement).scalars().all()]

    def add(self, record: ExecutionPlanRecord) -> ExecutionPlanRecord:
        plan = record.plan
        orm = PlanORM(
            plan_id=plan.plan_id.root,
            run_id=plan.run_id.root,
            task_id=plan.task_id.root,
            script_binding_id=plan.script_binding_id.root,
            script_definition_id=plan.script_definition_id.root,
            shard_id=plan.shard_id.root,
            attempt_id=plan.attempt_id.root,
            attempt_no=plan.attempt_no,
            project_id=plan.project_id.root,
            node_id=plan.node_id.root,
            target_session_id=plan.target_session_id.root,
            plan_hash=plan.plan_hash.root,
            deadline_at=plan.deadline_at,
            snapshot=plan.model_dump(mode="json"),
            created_at=plan.created_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)


__all__ = ["ExecutionPlanRepositoryImpl"]
