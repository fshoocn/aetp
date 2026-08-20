"""任务调度计划服务（P8.2，D-18）。

CRUD 管理任务定义的定时/周期调度计划；调度器按 next_run_at 推进并触发 Run。
cron_expression 与 interval_seconds 互斥二选一。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import croniter
from aetp_protocol.ids import new_id

from master.application.errors import TaskNotFoundError
from master.application.services.run_trigger_service import RunTriggerService
from master.domain.enums import TriggerType
from master.domain.models.task_schedule import TaskSchedule
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)

MAX_CRON_LENGTH = 128
MAX_INTERVAL_SECONDS = 365 * 24 * 3600  # 1 年


class ScheduleService:
    """任务调度计划 CRUD 与调度推进。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        trigger_service: RunTriggerService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._trigger = trigger_service

    def create_schedule(
        self,
        *,
        project_id: str,
        task_id: str,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        timezone: str = "UTC",
        enabled: bool = True,
        created_by: int,
    ) -> TaskSchedule:
        if not cron_expression and not interval_seconds:
            raise ValueError("必须提供 cron_expression 或 interval_seconds")
        if cron_expression and interval_seconds:
            raise ValueError("cron_expression 与 interval_seconds 互斥")
        if cron_expression and len(cron_expression) > MAX_CRON_LENGTH:
            raise ValueError("cron_expression 过长")
        if interval_seconds is not None and not 1 <= interval_seconds <= MAX_INTERVAL_SECONDS:
            raise ValueError("interval_seconds 必须在 1 到 31536000 之间")

        # 校验 cron 语法
        if cron_expression:
            try:
                croniter.croniter(cron_expression)
            except (ValueError, KeyError) as exc:
                raise ValueError(f"无效的 cron 表达式: {exc}") from exc

        with self._uow_factory() as uow:
            task = uow.test_tasks.get_by_task_id(task_id, project_id)
            if task is None:
                raise TaskNotFoundError(f"任务定义不存在: {task_id}")

            next_run = self._compute_next_run(
                cron_expression=cron_expression,
                interval_seconds=interval_seconds,
                timezone=timezone,
            )

            schedule = TaskSchedule(
                schedule_id=new_id(),
                task_id=task_id,
                project_id=project_id,
                cron_expression=cron_expression,
                interval_seconds=interval_seconds,
                timezone=timezone,
                enabled=enabled,
                next_run_at=next_run,
            )
            schedule = uow.task_schedules.add(schedule)

        logger.info(
            "调度计划已创建: schedule_id=%s task=%s cron=%s interval=%s",
            schedule.schedule_id, task_id, cron_expression, interval_seconds,
        )
        return schedule

    def list_schedules(self, task_id: str) -> list[TaskSchedule]:
        with self._uow_factory() as uow:
            return uow.task_schedules.list_by_task(task_id)

    def update_schedule(
        self,
        schedule_id: str,
        *,
        project_id: str,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        timezone: str | None = None,
        enabled: bool | None = None,
    ) -> TaskSchedule:
        with self._uow_factory() as uow:
            schedule = uow.task_schedules.get_by_schedule_id(schedule_id)
            if schedule is None or schedule.project_id != project_id:
                raise ValueError(f"调度计划不存在: {schedule_id}")

            if cron_expression is not None or interval_seconds is not None:
                new_cron = cron_expression if cron_expression is not None else schedule.cron_expression
                new_interval = interval_seconds if interval_seconds is not None else schedule.interval_seconds
                if not new_cron and not new_interval:
                    raise ValueError("必须提供 cron_expression 或 interval_seconds")
                if new_cron and new_interval:
                    raise ValueError("cron_expression 与 interval_seconds 互斥")
                if new_cron:
                    try:
                        croniter.croniter(new_cron)
                    except (ValueError, KeyError) as exc:
                        raise ValueError(f"无效的 cron 表达式: {exc}") from exc
                schedule.cron_expression = new_cron
                schedule.interval_seconds = new_interval
                schedule.next_run_at = self._compute_next_run(
                    cron_expression=new_cron,
                    interval_seconds=new_interval,
                    timezone=timezone or schedule.timezone,
                )

            if timezone is not None:
                schedule.timezone = timezone
            if enabled is not None:
                schedule.enabled = enabled

            return uow.task_schedules.update(schedule)

    def delete_schedule(self, schedule_id: str, project_id: str) -> None:
        with self._uow_factory() as uow:
            schedule = uow.task_schedules.get_by_schedule_id(schedule_id)
            if schedule is None or schedule.project_id != project_id:
                raise ValueError(f"调度计划不存在: {schedule_id}")
            uow.task_schedules.delete(schedule_id)
        logger.info("调度计划已删除: schedule_id=%s", schedule_id)

    async def tick(self, *, now: datetime | None = None) -> int:
        """调度器推进：触发所有到期计划，返回触发数量。"""
        now = now or utcnow()
        triggered = 0
        with self._uow_factory() as uow:
            due = uow.task_schedules.list_due(now=now, limit=50)
            for schedule in due:
                try:
                    await self._fire_schedule(uow, schedule, now)
                    triggered += 1
                except Exception:
                    logger.exception(
                        "调度计划触发失败: schedule_id=%s", schedule.schedule_id
                    )
        if triggered:
            logger.info("调度器本轮触发: %d 个计划", triggered)
        return triggered

    async def _fire_schedule(
        self, uow: UnitOfWork, schedule: TaskSchedule, now: datetime
    ) -> None:
        task = uow.test_tasks.get_by_task_id(schedule.task_id, schedule.project_id)
        if task is None or not task.enabled:
            schedule.enabled = False
            uow.task_schedules.update(schedule)
            logger.warning(
                "调度计划关联任务不存在或已停用，自动禁用: schedule=%s task=%s",
                schedule.schedule_id, schedule.task_id,
            )
            return

        # 触发 Run（async 契约，直接 await，不新建事件循环）
        if self._trigger is not None:
            await self._trigger.trigger(
                schedule.task_id,
                project_id=schedule.project_id,
                trigger_type=TriggerType.SCHEDULE,
                trigger_context={
                    "schedule_id": schedule.schedule_id,
                    "cron_expression": schedule.cron_expression,
                    "interval_seconds": schedule.interval_seconds,
                },
            )

        schedule.last_run_at = now
        schedule.next_run_at = self._compute_next_run(
            cron_expression=schedule.cron_expression,
            interval_seconds=schedule.interval_seconds,
            timezone=schedule.timezone,
            after=now,
        )
        uow.task_schedules.update(schedule)

    @staticmethod
    def _compute_next_run(
        *,
        cron_expression: str | None = None,
        interval_seconds: int | None = None,
        timezone: str = "UTC",
        after: datetime | None = None,
    ) -> datetime | None:
        after = after or utcnow()
        if cron_expression:
            cron = croniter.croniter(cron_expression, after)
            return cron.get_next(datetime)
        if interval_seconds is not None and interval_seconds > 0:
            return after + timedelta(seconds=interval_seconds)
        return None
