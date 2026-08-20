"""SQLAlchemy Run 级汇总投影仓储实现（P3.4，results 表，run_pk 唯一）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import RunResult as RunResultORM
from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM
from master.adapters.sqlalchemy.orm import TestTask as TestTaskORM
from master.domain.enums import RunStatus
from master.domain.models import RunResult
from master.domain.repositories import RunResultRepository


def _to_domain(orm: RunResultORM) -> RunResult:
    return RunResult(
        id=orm.id,
        result_id=orm.result_id,
        run_id=orm.run.run_id if orm.run is not None else "",
        project_id=orm.project.project_id if orm.project is not None else "",
        task_id=orm.task.task_id if orm.task is not None else "",
        node_id=orm.node_id,
        passed=orm.passed,
        status=RunStatus(orm.status),
        metrics=dict(orm.metrics) if orm.metrics else None,
        data=dict(orm.data) if orm.data else None,
        started_at=orm.started_at,
        finished_at=orm.finished_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class RunResultRepositoryImpl(RunResultRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, result: RunResult) -> RunResult:
        run_pk = self._s.execute(
            select(TaskRunORM.id).where(TaskRunORM.run_id == result.run_id)
        ).scalar_one_or_none()
        if run_pk is None:
            raise ValueError(f"Run 不存在: {result.run_id}")
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == result.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {result.project_id}")
        task_pk = self._s.execute(
            select(TestTaskORM.id).where(TestTaskORM.task_id == result.task_id)
        ).scalar_one_or_none()
        if task_pk is None:
            raise ValueError(f"任务定义不存在: {result.task_id}")
        orm = RunResultORM(
            result_id=result.result_id,
            run_pk=run_pk,
            project_pk=project_pk,
            task_pk=task_pk,
            node_id=result.node_id,
            passed=result.passed,
            status=result.status.value,
            metrics=result.metrics,
            data=result.data,
            started_at=result.started_at,
            finished_at=result.finished_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_run_id(self, run_id: str) -> RunResult | None:
        orm = self._s.execute(
            select(RunResultORM)
            .options(
                joinedload(RunResultORM.run),
                joinedload(RunResultORM.project),
                joinedload(RunResultORM.task),
            )
            .where(
                RunResultORM.run_pk
                == select(TaskRunORM.id)
                .where(TaskRunORM.run_id == run_id)
                .scalar_subquery()
            )
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def update(self, result: RunResult) -> RunResult:
        orm = self._s.get(RunResultORM, result.id)
        if orm is None:
            raise ValueError(f"Run 汇总不存在: id={result.id}")
        orm.node_id = result.node_id
        orm.passed = result.passed
        orm.status = result.status.value
        orm.metrics = result.metrics
        orm.data = result.data
        orm.started_at = result.started_at
        orm.finished_at = result.finished_at
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def nullify_task_for_results(self, task_id: str) -> int:
        """把引用指定任务定义的 Run 汇总投影的 task_pk 置空（保留历史）。

        Returns:
            受影响的 Run 汇总数量
        """
        from typing import Any

        from sqlalchemy import update as sa_update
        from sqlalchemy.engine import Result
        result: Result[Any] = self._s.execute(
            sa_update(RunResultORM)
            .where(
                RunResultORM.task_pk
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
