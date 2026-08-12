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
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """广播领域事件给所有订阅者。"""
        event = DomainEvent(
            type=event_type,
            data=data,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        async with self._lock:
            for queue in list(self._subscribers):
                queue.put_nowait(event)

    async def subscribe(self) -> asyncio.Queue:
        """注册一个订阅者队列（收到所有事件，由消费方按 type 过滤）。"""
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """注销订阅者队列。"""
        async with self._lock:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
