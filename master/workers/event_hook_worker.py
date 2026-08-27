"""事件 Hook 异步消费 worker（§10.6 补充）。

领域事件持久化后，Event Hook 不应阻塞 SSE 广播与通知投递。本 worker
维护一个有界队列，``EventPublisher`` 将事件**非阻塞入队**，后台循环
异步执行匹配的 Event Hook（fail open），并把执行结果写 ``hook_executions``
审计（复用 ``HookRunner.run_event_hooks`` 的审计语义）。

队列满时丢弃该事件并记录告警（Event Hook 是旁路增强，不保证逐条送达）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from master.application.services.hook_runner import HookRunner
from master.domain.models import DomainEvent

logger = logging.getLogger("master.workers.event_hook")

DEFAULT_QUEUE_SIZE = 1024


class EventHookWorker:
    """后台消费领域事件、执行 Event Hook。"""

    def __init__(
        self,
        hook_runner: HookRunner,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._runner = hook_runner
        self._queue: asyncio.Queue[DomainEvent] = asyncio.Queue(maxsize=queue_size)
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def enqueue(self, event: DomainEvent) -> bool:
        """非阻塞入队；队满丢弃并返回 False。"""
        try:
            self._queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            logger.warning("事件 Hook 队列已满，丢弃事件: event=%s", event.event_type)
            return False

    async def start(self) -> None:
        """启动后台消费循环（幂等）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("事件 Hook worker 启动")

    async def stop(self) -> None:
        """停止消费循环（幂等）。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("事件 Hook worker 停止")

    async def drain_once(self) -> bool:
        """从队列取一条事件并执行 Hook（用于测试/显式消费）。返回是否取到。"""
        try:
            event = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        try:
            await self._runner.run_event_hooks(event)
        except Exception:
            logger.exception("事件 Hook 消费异常: event=%s", event.event_type)
        finally:
            self._queue.task_done()
        return True

    async def _loop(self) -> None:
        while self._running:
            event = await self._queue.get()
            try:
                await self._runner.run_event_hooks(event)
            except Exception:
                # HookRunner.run_event_hooks 已对单个 hook 异常 fail open，
                # 这里兜底防御，确保消费循环永不因异常退出。
                logger.exception("事件 Hook 消费异常: event=%s", event.event_type)
            finally:
                self._queue.task_done()
