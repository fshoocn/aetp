"""领域对象：Hook 执行审计（P8.4，§10.6）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class HookExecution:
    """一次 Hook 执行记录（准入或事件 Hook）。"""

    id: int | None = None
    execution_id: str = ""
    event_id: str | None = None
    project_id: str | None = None
    hook_name: str = ""
    stage: str = ""
    status: str = ""
    duration_ms: float | None = None
    error_message: str | None = None
    occurred_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
