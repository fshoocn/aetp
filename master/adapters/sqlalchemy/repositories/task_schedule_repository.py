"""SQLAlchemy 任务调度计划仓储实现。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import TaskSchedule as ScheduleORM
from master.adapters.sqlalchemy.orm import TestTask as TaskORM
from master.domain.models.task_schedule import TaskSchedule
from master.domain.repositories import TaskScheduleRepository


def _to_domain(orm: ScheduleORM) -> TaskSchedule:
    task = orm.task
    return TaskSchedule(
        id=orm.id,
        schedule_id=orm.schedule_id,
        task_id=task.task_id if task is not None else "",
        project_id=task.project.project_id if task is not None and task.project is not None else "",
        cron_expression=orm.cron_expression,
        interval_seconds=orm.interval_seconds,
        timezone=orm.timezone,
        enabled=orm.enabled,
        next_run_at=orm.next_run_at,
        last_run_at=orm.last_run_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class TaskScheduleRepositoryImpl(TaskScheduleRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_schedule_id(self, schedule_id: str) -> TaskSchedule | None:
        orm = (
            self._s.execute(
                select(ScheduleORM).options(joinedload(ScheduleORM.task)).where(ScheduleORM.schedule_id == schedule_id)
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def list_by_task(self, task_id: str) -> list[TaskSchedule]:
        stmt = (
            select(ScheduleORM)
            .options(joinedload(ScheduleORM.task))
            .where(ScheduleORM.task_pk == select(TaskORM.id).where(TaskORM.task_id == task_id).scalar_subquery())
            .order_by(ScheduleORM.id)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def list_due(self, *, now: datetime, limit: int = 100) -> list[TaskSchedule]:
        stmt = (
            select(ScheduleORM)
            .options(joinedload(ScheduleORM.task))
            .where(ScheduleORM.enabled.is_(True))
            .where(ScheduleORM.next_run_at.is_not(None))
            .where(ScheduleORM.next_run_at <= now)
            .order_by(ScheduleORM.next_run_at)
            .limit(limit)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, schedule: TaskSchedule) -> TaskSchedule:
        task_pk = self._s.execute(select(TaskORM.id).where(TaskORM.task_id == schedule.task_id)).scalar_one_or_none()
        if task_pk is None:
            raise ValueError(f"任务定义不存在: {schedule.task_id}")
        orm = ScheduleORM(
            schedule_id=schedule.schedule_id,
            task_pk=task_pk,
            cron_expression=schedule.cron_expression,
            interval_seconds=schedule.interval_seconds,
            timezone=schedule.timezone,
            enabled=schedule.enabled,
            next_run_at=schedule.next_run_at,
            last_run_at=schedule.last_run_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def update(self, schedule: TaskSchedule) -> TaskSchedule:
        orm = self._s.get(ScheduleORM, schedule.id)
        if orm is None:
            raise ValueError(f"调度计划不存在: id={schedule.id}")
        orm.cron_expression = schedule.cron_expression
        orm.interval_seconds = schedule.interval_seconds
        orm.timezone = schedule.timezone
        orm.enabled = schedule.enabled
        orm.next_run_at = schedule.next_run_at
        orm.last_run_at = schedule.last_run_at
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def delete(self, schedule_id: str) -> None:
        orm = self._s.execute(select(ScheduleORM).where(ScheduleORM.schedule_id == schedule_id)).scalars().one_or_none()
        if orm is None:
            raise ValueError(f"调度计划不存在: {schedule_id}")
        self._s.delete(orm)
        self._s.flush()
