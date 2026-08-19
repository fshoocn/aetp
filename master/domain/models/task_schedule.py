"""领域对象：任务调度计划（D-18，§18.7）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class TaskSchedule:
    """项目内任务定义的定时/周期调度计划。

    cron_expression 与 interval_seconds 互斥二选一（D-18）。
    调度器按 next_run_at 推进，触发后更新 last_run_at 并计算下次执行时间。
    """

    id: int | None = None
    schedule_id: str = ""
    task_id: str = ""
    project_id: str = ""
    cron_expression: str | None = None
    interval_seconds: int | None = None
    timezone: str = "UTC"
    enabled: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
