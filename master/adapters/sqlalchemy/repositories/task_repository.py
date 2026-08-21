"""SQLAlchemy 任务仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import Device as DeviceORM
from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import Task as TaskORM
from master.domain.enums import TaskStatus
from master.domain.models import Task
from master.domain.repositories import TaskRepository


def _to_domain(orm: TaskORM) -> Task:
    return Task(
        id=orm.id,
        task_id=orm.task_id,
        project_id=orm.project.project_id if orm.project is not None else "",
        device_id=orm.device.device_id if orm.device is not None else "",
        status=TaskStatus(orm.status),
        command=dict(orm.command or {}),
        result=dict(orm.result) if orm.result is not None else None,
        error=orm.error,
        created_by=orm.created_by,
        started_at=orm.started_at,
        finished_at=orm.finished_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class TaskRepositoryImpl(TaskRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, task: Task) -> Task:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == task.project_id)
        ).scalar_one_or_none()
        device_pk = self._s.execute(
            select(DeviceORM.id).where(DeviceORM.device_id == task.device_id)
        ).scalar_one_or_none()
        if project_pk is None or device_pk is None:
            raise ValueError("项目或设备不存在")
        orm = TaskORM(
            task_id=task.task_id,
            project_pk=project_pk,
            device_pk=device_pk,
            created_by=task.created_by,
            status=task.status.value,
            command=task.command,
            result=task.result,
            error=task.error,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )
        self._s.add(orm)
        self._s.flush()
        return _to_domain(orm)

    def get_by_task_id(self, task_id: str, project_id: str | None = None) -> Task | None:
        stmt = (
            select(TaskORM)
            .options(joinedload(TaskORM.project), joinedload(TaskORM.device))
            .where(TaskORM.task_id == task_id)
        )
        if project_id is not None:
            stmt = stmt.where(
                TaskORM.project_pk == select(ProjectORM.id).where(ProjectORM.project_id == project_id).scalar_subquery()
            )
        orm = self._s.execute(stmt).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list(
        self,
        *,
        project_id: str | None = None,
        device_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Task]:
        stmt = (
            select(TaskORM)
            .options(joinedload(TaskORM.project), joinedload(TaskORM.device))
            .order_by(TaskORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if project_id is not None:
            stmt = stmt.where(
                TaskORM.project_pk == select(ProjectORM.id).where(ProjectORM.project_id == project_id).scalar_subquery()
            )
        if device_id:
            stmt = stmt.where(
                TaskORM.device_pk == select(DeviceORM.id).where(DeviceORM.device_id == device_id).scalar_subquery()
            )
        if status:
            stmt = stmt.where(TaskORM.status == status)
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def update(self, task: Task) -> Task:
        orm = self._s.get(TaskORM, task.id)
        if orm is None:
            raise ValueError(f"任务不存在: id={task.id}")
        orm.status = task.status.value
        orm.command = task.command
        orm.result = task.result
        orm.error = task.error
        orm.started_at = task.started_at
        orm.finished_at = task.finished_at
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
