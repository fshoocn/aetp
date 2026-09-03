"""任务调度计划仓储实现。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import TaskSchedule as ScheduleORM
from master.domain.models.task_schedule import TaskSchedule
from master.domain.repositories import TaskScheduleRepository


def _to_domain(orm: ScheduleORM) -> TaskSchedule:
    return TaskSchedule(
        id=orm.id,
        schedule_id=orm.schedule_id,
        task_id=orm.task_id,
        project_id=orm.project_id,
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
        orm = self._s.execute(
            select(ScheduleORM).where(ScheduleORM.schedule_id == schedule_id)
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list_by_task(self, task_id: str) -> list[TaskSchedule]:
        statement = (
            select(ScheduleORM)
            .where(ScheduleORM.task_id == task_id)
            .order_by(ScheduleORM.id)
        )
        return [_to_domain(orm) for orm in self._s.execute(statement).scalars().all()]

    def list_due(self, *, now: datetime, limit: int = 100) -> list[TaskSchedule]:
        statement = (
            select(ScheduleORM)
            .where(ScheduleORM.enabled.is_(True))
            .where(ScheduleORM.next_run_at.is_not(None))
            .where(ScheduleORM.next_run_at <= now)
            .order_by(ScheduleORM.next_run_at)
            .limit(limit)
        )
        return [_to_domain(orm) for orm in self._s.execute(statement).scalars().all()]

    def add(self, schedule: TaskSchedule) -> TaskSchedule:
        orm = ScheduleORM(
            schedule_id=schedule.schedule_id,
            task_id=schedule.task_id,
            project_id=schedule.project_id,
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
        orm = self._s.execute(
            select(ScheduleORM).where(ScheduleORM.schedule_id == schedule_id)
        ).scalars().one_or_none()
        if orm is None:
            raise ValueError(f"调度计划不存在: {schedule_id}")
        self._s.delete(orm)
        self._s.flush()
