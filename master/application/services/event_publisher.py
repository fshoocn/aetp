"""持久化领域事件并广播到进程内 SSE 总线（P7.1），同时分发通知（P8.5）。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from aetp_protocol.ids import new_id

from master.adapters.sse.event_bus import EventBus
from master.application.services.notification_dispatcher import NotificationDispatcher
from master.domain.models import DomainEvent
from master.domain.repositories import UnitOfWork
from master.workers.event_hook_worker import EventHookWorker

logger = logging.getLogger(__name__)


class EventPublisher:
    """领域事件发布端口的 Master 实现。

    先提交 ``domain_events``，再广播实时事件；这样 SSE 断线后可以用
    Last-Event-ID 从数据库恢复，而不会只依赖进程内队列。
    """

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        event_bus: EventBus,
        *,
        notification_dispatcher: NotificationDispatcher | None = None,
        event_hook_worker: EventHookWorker | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._event_bus = event_bus
        self._dispatcher: NotificationDispatcher | None = notification_dispatcher
        self._event_hook_worker = event_hook_worker

    async def publish(
        self,
        event_type: str,
        data: Mapping[str, Any] | None = None,
        *,
        project_id: str | None = None,
        aggregate_id: str | None = None,
    ) -> DomainEvent:
        """持久化并广播一个领域事件。"""
        payload = dict(data or {})
        resolved_project_id = project_id or _string_value(payload, "project_id")
        resolved_aggregate_id = (
            aggregate_id
            or _string_value(payload, "run_id")
            or _string_value(payload, "task_id")
            or _string_value(payload, "node_id")
            or event_type
        )
        event = DomainEvent(
            event_id=new_id(),
            project_id=resolved_project_id,
            event_type=event_type,
            aggregate_id=resolved_aggregate_id,
            payload=payload,
            occurred_at=datetime.now(UTC),
        )
        with self._uow_factory() as uow:
            persisted = uow.domain_events.add(event)

        await self.broadcast(persisted)
        self._enqueue_event_hook(persisted)
        await self._dispatch_to_notifications(persisted)
        return persisted

    async def broadcast(self, event: DomainEvent) -> None:
        """广播已经持久化的领域事件，不再次写入数据库。"""
        await self._event_bus.publish(
            event.event_type,
            event.payload,
            event_id=event.event_id,
            sequence=event.sequence,
            project_id=event.project_id,
            occurred_at=event.occurred_at,
        )

    async def _dispatch_to_notifications(self, event: DomainEvent) -> None:
        """将事件分发给通知 dispatcher（失败不阻塞主流程）。"""
        if self._dispatcher is None:
            return
        try:
            await self._dispatcher.dispatch(event)
        except Exception:
            logger.exception("通知分发失败: event=%s", event.event_type)

    def _enqueue_event_hook(self, event: DomainEvent) -> None:
        """将事件非阻塞入队到事件 Hook worker（旁路增强，失败不影响主流程）。"""
        if self._event_hook_worker is None:
            return
        try:
            self._event_hook_worker.enqueue(event)
        except Exception:
            logger.exception("事件 Hook 入队失败: event=%s", event.event_type)


def _string_value(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None
