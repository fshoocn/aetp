"""通知分发器（P8.5，§10.5）。

接收领域事件，匹配事件订阅，路由到对应 sender adapter 投递。
每次投递写 event_deliveries 记录，支持幂等 (event_id, subscription_id)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aetp_protocol.ids import new_id
from sqlalchemy.exc import IntegrityError

from master.adapters.notifications.senders import SenderRegistry
from master.domain.models import DomainEvent
from master.domain.models.notification import EventDelivery
from master.domain.notifications import NotificationEndpoint, NotificationMessage
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """领域事件 → 通知投递。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        registry: SenderRegistry,
        *,
        get_secret: Callable[[str], str | None] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._get_secret = get_secret or (lambda _: None)

    async def dispatch(self, event: DomainEvent) -> int:
        """投递一个领域事件到所有匹配的订阅，返回成功投递数。"""
        delivered = 0

        with self._uow_factory() as uow:
            subscriptions = uow.event_subscriptions.list_by_project(event.project_id or "", limit=1000)

        for sub in subscriptions:
            if not sub.enabled:
                continue
            if sub.event_types and event.event_type not in sub.event_types:
                continue
            if sub.task_id and event.payload.get("task_id") != sub.task_id:
                continue
            if not self._matches_filter(event, sub.filter_json):
                continue

            delivery = EventDelivery(
                delivery_id=new_id(),
                project_id=event.project_id or "",
                event_id=event.event_id,
                subscription_id=sub.subscription_id,
                endpoint_id=sub.endpoint_id,
                content={
                    "event_type": event.event_type,
                    "event_id": event.event_id,
                    "task_id": event.payload.get("task_id"),
                    "aggregate_id": event.aggregate_id,
                    "project_id": event.project_id,
                    "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
                    "payload": event.payload,
                },
                status="pending",
            )
            try:
                with self._uow_factory() as uow:
                    existing = uow.event_deliveries.get_by_event_subscription(event.event_id, sub.subscription_id)
                    if existing is not None:
                        # 已存在记录：终态（succeeded/failed/exhausted）视为已处理跳过；
                        # pending 视为上次投递未完成（进程崩溃遗留），接管重投。
                        if existing.status not in ("pending",):
                            continue
                        delivery = existing
                    else:
                        delivery = uow.event_deliveries.add(delivery)
            except IntegrityError:
                logger.info(
                    "通知投递已被并发分发器占用: subscription=%s event=%s",
                    sub.subscription_id,
                    event.event_id,
                )
                continue

            try:
                result = await self._deliver(event, sub.endpoint_id)
                status = result.status
                error_message = None if status == "succeeded" else result.detail
            except Exception as exc:
                logger.exception(
                    "Notification dispatch error: subscription=%s event=%s",
                    sub.subscription_id,
                    event.event_type,
                )
                status = "failed"
                error_message = str(exc)[:500]

            delivery.status = status
            delivery.attempts += 1
            delivery.sent_at = utcnow() if status == "succeeded" else None
            delivery.error_message = error_message
            with self._uow_factory() as uow:
                uow.event_deliveries.update(delivery)
            if status == "succeeded":
                delivered += 1

        if delivered:
            logger.info("通知投递完成: event=%s type=%s delivered=%d", event.event_id, event.event_type, delivered)
        return delivered

    @staticmethod
    def _matches_filter(event: DomainEvent, filters: dict) -> bool:
        """兼容旧订阅的 payload 顶层字段筛选。"""
        return all(event.payload.get(key) == expected for key, expected in (filters or {}).items())

    async def _deliver(self, event: DomainEvent, endpoint_id: str):
        with self._uow_factory() as uow:
            ep_model = uow.notification_endpoints.get_by_endpoint_id(endpoint_id)
            if ep_model is None or not ep_model.enabled:
                raise ValueError(f"端点不存在或已禁用: {endpoint_id}")

        # 转换为协议端点（senders 只访问 config/name/channel_type）
        ep = NotificationEndpoint(
            endpoint_id=ep_model.endpoint_id,
            channel_type=ep_model.channel_type,
            name=ep_model.name,
            config=ep_model.config or {},
            secret_ref=ep_model.secret_ref,
        )

        channel_type = ep.channel_type
        sender = self._registry.get(channel_type)
        if sender is None:
            raise ValueError(f"未注册的 sender: {channel_type}")

        payload = event.payload
        task_id = payload.get("task_id") or "未绑定任务"
        if event.event_type == "run.progress":
            body = (
                f"任务 {task_id} 执行进度 {payload.get('percent', 0)}%"
                f" · {payload.get('stage', '')} · {payload.get('message', '')}"
            )
            severity = "info"
        elif event.event_type == "run.result":
            body = f"任务 {task_id} 执行完成：{'通过' if payload.get('passed') else '失败'}"
            severity = "info" if payload.get("passed") else "error"
        else:
            body = f"任务 {task_id}：事件 {event.event_type} 发生于 {event.occurred_at}"
            severity = "info"
        message = NotificationMessage(
            subject=f"[AETP] {event.event_type}",
            body=body,
            severity=severity,
            event=event,
        )

        secret_value = self._get_secret(ep_model.secret_ref) if ep_model.secret_ref else None
        return await sender.send(message, ep, secret_value=secret_value)
