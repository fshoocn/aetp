"""SQLAlchemy Run 执行仓储实现（P3.4，task_runs 表）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM
from master.adapters.sqlalchemy.orm import TestTask as TestTaskORM
from master.adapters.sqlalchemy.orm import User as UserORM
from master.domain.enums import RunStatus, TriggerType
from master.domain.models import TaskRun
from master.domain.repositories import TaskRunRepository


def _to_domain(orm: TaskRunORM) -> TaskRun:
    return TaskRun(
        id=orm.id,
        run_id=orm.run_id,
        project_id=orm.project.project_id if orm.project is not None else "",
        task_id=orm.task.task_id if orm.task is not None else "",
        script_ref=dict(orm.script_ref or {}),
        case_selection=list(orm.case_selection or []),
        split_policy=dict(orm.split_policy or {}),
        trigger_type=TriggerType(orm.trigger_type),
        triggered_by_user_id=orm.triggered_by_user_pk,
        integration_id=orm.integration_id,
        trigger_context=dict(orm.trigger_context) if orm.trigger_context else None,
        status=RunStatus(orm.status),
        started_at=orm.started_at,
        finished_at=orm.finished_at,
        log_complete=orm.log_complete,
        last_log_sequence=orm.last_log_sequence,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class TaskRunRepositoryImpl(TaskRunRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, run: TaskRun) -> TaskRun:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == run.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {run.project_id}")
        task_pk = self._s.execute(
            select(TestTaskORM.id).where(TestTaskORM.task_id == run.task_id)
        ).scalar_one_or_none()
        if task_pk is None:
            raise ValueError(f"任务定义不存在: {run.task_id}")
        triggered_by_user_pk = None
        if run.triggered_by_user_id is not None:
            triggered_by_user_pk = self._s.execute(
                select(UserORM.id).where(UserORM.id == run.triggered_by_user_id)
            ).scalar_one_or_none()
            if triggered_by_user_pk is None:
                raise ValueError(f"触发用户不存在: {run.triggered_by_user_id}")
        orm = TaskRunORM(
            run_id=run.run_id,
            project_pk=project_pk,
            task_pk=task_pk,
            script_ref=run.script_ref,
            case_selection=run.case_selection,
            split_policy=run.split_policy,
            trigger_type=run.trigger_type.value,
            triggered_by_user_pk=triggered_by_user_pk,
            integration_id=run.integration_id,
            trigger_context=run.trigger_context,
            status=run.status.value,
            started_at=run.started_at,
            finished_at=run.finished_at,
            log_complete=run.log_complete,
            last_log_sequence=run.last_log_sequence,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_run_id(
        self, run_id: str, project_id: str | None = None
    ) -> TaskRun | None:
        stmt = (
            select(TaskRunORM)
            .options(
                joinedload(TaskRunORM.project),
                joinedload(TaskRunORM.task),
            )
            .where(TaskRunORM.run_id == run_id)
        )
        if project_id is not None:
            stmt = stmt.where(
                TaskRunORM.project_pk
                == select(ProjectORM.id)
                .where(ProjectORM.project_id == project_id)
                .scalar_subquery()
            )
        orm = self._s.execute(stmt).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        trigger_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskRun]:
        stmt = (
            select(TaskRunORM)
            .options(
                joinedload(TaskRunORM.project),
                joinedload(TaskRunORM.task),
            )
            .order_by(TaskRunORM.created_at.desc(), TaskRunORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if project_id is not None:
            stmt = stmt.where(
                TaskRunORM.project_pk
                == select(ProjectORM.id)
                .where(ProjectORM.project_id == project_id)
                .scalar_subquery()
            )
        if task_id is not None:
            stmt = stmt.where(
                TaskRunORM.task_pk
                == select(TestTaskORM.id)
                .where(TestTaskORM.task_id == task_id)
                .scalar_subquery()
            )
        if status is not None:
            stmt = stmt.where(TaskRunORM.status == status)
        if trigger_type is not None:
            stmt = stmt.where(TaskRunORM.trigger_type == trigger_type)
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def update(self, run: TaskRun) -> TaskRun:
        orm = self._s.get(TaskRunORM, run.id)
        if orm is None:
            raise ValueError(f"Run 不存在: id={run.id}")
        orm.status = run.status.value
        orm.started_at = run.started_at
        orm.finished_at = run.finished_at
        orm.integration_id = run.integration_id
        orm.trigger_context = run.trigger_context
        orm.log_complete = run.log_complete
        orm.last_log_sequence = run.last_log_sequence
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def list_non_terminal(self, limit: int = 1000) -> list[TaskRun]:
        """查询所有非终态的 Run（启动恢复/超时检测用）。"""
        stmt = (
            select(TaskRunORM)
            .options(
                joinedload(TaskRunORM.project),
                joinedload(TaskRunORM.task),
            )
            .where(
                TaskRunORM.status.in_([
                    RunStatus.CREATED.value,
                    RunStatus.DISPATCHED.value,
                    RunStatus.ACKED.value,
                    RunStatus.RUNNING.value,
                ])
            )
            .order_by(TaskRunORM.id)
            .limit(limit)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def nullify_task_for_runs(self, task_id: str) -> int:
        """把引用指定任务定义的所有 Run 的 task_pk 置空（保留历史）。

        Returns:
            受影响的 Run 数量
        """
        from typing import Any

        from sqlalchemy import update as sa_update
        from sqlalchemy.engine import Result
        result: Result[Any] = self._s.execute(
            sa_update(TaskRunORM)
            .where(
                TaskRunORM.task_pk
                == select(TestTaskORM.id)
                .where(TestTaskORM.task_id == task_id)
                .scalar_subquery()
            )
            .values(task_pk=None)
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            self._s.flush()
        return count
