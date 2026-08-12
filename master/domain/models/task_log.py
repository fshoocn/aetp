"""领域对象：测试任务日志。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class TaskLog:
    """任务执行日志行。"""

    id: int | None
    task_id: str
    sequence: int
    level: str
    message: str
    ts: datetime
    created_at: datetime | None = None
    updated_at: datetime | None = None
