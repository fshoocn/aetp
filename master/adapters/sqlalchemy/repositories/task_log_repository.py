"""SQLAlchemy 任务日志仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import Task as TaskORM
from master.adapters.sqlalchemy.orm import TaskLog as TaskLogORM
from master.domain.models import TaskLog
from master.domain.repositories import TaskLogRepository


def _to_domain(orm: TaskLogORM) -> TaskLog:
    return TaskLog(
        id=orm.id,
        task_id=orm.task.task_id if orm.task is not None else "",
        sequence=orm.sequence,
        level=orm.level,
        message=orm.message,
        ts=orm.ts,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class TaskLogRepositoryImpl(TaskLogRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_by_task(self, task_id: str, project_id: str | None = None) -> list[TaskLog]:
        stmt = (
            select(TaskLogORM)
            .join(TaskORM, TaskORM.id == TaskLogORM.task_pk)
            .where(TaskORM.task_id == task_id)
            .order_by(TaskLogORM.sequence)
        )
        if project_id is not None:
            stmt = stmt.where(
                TaskORM.project_pk == select(ProjectORM.id).where(ProjectORM.project_id == project_id).scalar_subquery()
            )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add_many(self, logs: list[TaskLog]) -> list[TaskLog]:
        persisted: list[TaskLog] = []
        for log in logs:
            task_pk = self._s.execute(select(TaskORM.id).where(TaskORM.task_id == log.task_id)).scalar_one_or_none()
            if task_pk is None:
                raise ValueError(f"任务不存在: {log.task_id}")
            orm = TaskLogORM(
                task_pk=task_pk,
                sequence=log.sequence,
                level=log.level,
                message=log.message,
                ts=log.ts,
            )
            self._s.add(orm)
            self._s.flush()
            persisted.append(_to_domain(orm))
        return persisted
