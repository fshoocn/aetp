"""领域事件数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DomainEvent:
    """SSE 推送的领域事件载荷。

    type: 事件类型（如 task.created / task.updated）
    data: 业务数据（已序列化为 JSON 安全结构）
    ts: 事件产生时间（ISO 8601）
    """

    type: str
    data: dict[str, Any]
    ts: str = ""
    event_id: str = ""
    sequence: int | None = None
    project_id: str | None = None
