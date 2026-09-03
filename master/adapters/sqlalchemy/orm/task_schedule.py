"""ORM：任务调度计划（D-18，§18.7）。

cron_expression 与 interval_seconds 互斥二选一（CHECK 约束）。
调度器按 next_run_at 推进，触发后更新 last_run_at 并计算下次执行时间。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UTCDateTime


class TaskSchedule(Base, TimestampMixin):
    __tablename__ = "task_schedules"
    __table_args__ = (
        CheckConstraint(
            "(cron_expression IS NULL AND interval_seconds IS NOT NULL) OR "
            "(cron_expression IS NOT NULL AND interval_seconds IS NULL)",
            name="ck_task_schedules_cron_or_interval",
        ),
        Index("ix_task_schedules_enabled_next", "enabled", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cron_expression: Mapped[str | None] = mapped_column(String(128), nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
