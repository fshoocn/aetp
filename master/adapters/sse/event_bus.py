"""进程内异步事件总线（SSE 数据源）。

EventBus 提供 pub/sub：
- 业务侧（如任务创建/状态变更）调用 publish() 广播事件；
- SSE 端点 subscribe() 订阅事件流，逐个推送。

实现基于 asyncio 队列；单进程部署下可用。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from master.adapters.sse.event import DomainEvent


class EventBus:
    """轻量级异步事件总线。"""

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue, str] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        event_id: str = "",
        sequence: int | None = None,
        project_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """按项目广播领域事件给实时订阅者。"""
        event = DomainEvent(
            type=event_type,
            data=data,
            ts=(occurred_at or datetime.now(timezone.utc)).isoformat(),
            event_id=event_id,
            sequence=sequence,
            project_id=project_id,
        )
        async with self._lock:
            for queue, subscriber_project_id in list(self._subscribers.items()):
                if project_id is not None and project_id == subscriber_project_id:
                    queue.put_nowait(event)

    async def subscribe(self, project_id: str) -> asyncio.Queue:
        """注册一个项目范围订阅者队列。"""
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers[queue] = project_id
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """注销订阅者队列。"""
        async with self._lock:
            self._subscribers.pop(queue, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
