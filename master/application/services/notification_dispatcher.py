"""通知分发器（P8.5，§10.5）。

接收领域事件，匹配事件订阅，路由到对应 sender adapter 投递。
每次投递写 event_deliveries 记录，支持幂等 (event_id, subscription_id)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from aetp_protocol.ids import new_id
from aetp_protocol.reporting import NotificationPolicy
from sqlalchemy.exc import IntegrityError

from master.adapters.notifications.senders import SenderRegistry
from master.domain.models import DomainEvent
from master.domain.models.notification import EventDelivery
from master.domain.notifications import NotificationEndpoint, NotificationMessage
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)

_TERMINAL_RUN_EVENTS = frozenset({"run.result", "run.finished", "run.succeeded", "run.failed", "run.completed"})
_AGGREGATED_STATUS = "aggregated"
_COALESCED_STATUS = "coalesced"


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

            try:
                policy = NotificationPolicy.model_validate(sub.throttle_policy or {})
            except ValueError as exc:
                logger.warning("通知策略无效，跳过投递: subscription=%s error=%s", sub.subscription_id, exc)
                continue
            dedupe_key = _dedupe_key(policy, event)
            if policy.mode == "digest":
                window_ends_at = _window_ends_at(event.occurred_at, policy.window_s)
                self._record_aggregate(
                    event,
                    sub.subscription_id,
                    sub.endpoint_id,
                    dedupe_key=dedupe_key,
                    aggregation_key=_aggregation_key(sub.subscription_id, event.occurred_at, policy.window_s),
                    window_ends_at=window_ends_at,
                )
                continue
            if policy.mode == "run_summary" and event.event_type not in _TERMINAL_RUN_EVENTS:
                self._record_aggregate(
                    event,
                    sub.subscription_id,
                    sub.endpoint_id,
                    dedupe_key=dedupe_key,
                    aggregation_key=_run_aggregation_key(event),
                )
                continue

            if policy.mode == "run_summary":
                prepared = self._prepare_run_summary(event, sub.subscription_id, sub.endpoint_id, policy, dedupe_key)
                if prepared is None:
                    continue
                delivery, message = prepared
            else:
                try:
                    delivery = self._reserve_delivery(
                        event,
                        sub.subscription_id,
                        sub.endpoint_id,
                        dedupe_key=dedupe_key,
                    )
                except IntegrityError:
                    logger.info(
                        "通知投递已被并发分发器占用: subscription=%s event=%s",
                        sub.subscription_id,
                        event.event_id,
                    )
                    continue
                if delivery is None:
                    continue
                message = None

            try:
                result = await self._deliver(event, sub.endpoint_id, message=message)
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

    def _reserve_delivery(
        self,
        event: DomainEvent,
        subscription_id: str,
        endpoint_id: str,
        *,
        dedupe_key: str,
    ) -> EventDelivery | None:
        with self._uow_factory() as uow:
            existing = self._find_existing(uow, event.event_id, subscription_id, dedupe_key)
            if existing is not None:
                if existing.status not in ("pending",):
                    return None
                return existing
            return uow.event_deliveries.add(
                self._new_delivery(
                    event,
                    subscription_id,
                    endpoint_id,
                    dedupe_key=dedupe_key,
                )
            )

    def _record_aggregate(
        self,
        event: DomainEvent,
        subscription_id: str,
        endpoint_id: str,
        *,
        dedupe_key: str,
        aggregation_key: str,
        window_ends_at: datetime | None = None,
    ) -> bool:
        try:
            with self._uow_factory() as uow:
                existing = self._find_existing(uow, event.event_id, subscription_id, dedupe_key)
                if existing is not None:
                    return True
                uow.event_deliveries.add(
                    self._new_delivery(
                        event,
                        subscription_id,
                        endpoint_id,
                        dedupe_key=dedupe_key,
                        status=_AGGREGATED_STATUS,
                        aggregation_key=aggregation_key,
                        window_ends_at=window_ends_at,
                    )
                )
        except IntegrityError:
            logger.info(
                "通知聚合已被并发分发器占用: subscription=%s event=%s",
                subscription_id,
                event.event_id,
            )
        return True

    def _prepare_run_summary(
        self,
        event: DomainEvent,
        subscription_id: str,
        endpoint_id: str,
        policy: NotificationPolicy,
        dedupe_key: str,
    ) -> tuple[EventDelivery, NotificationMessage] | None:
        aggregation_key = _run_aggregation_key(event)
        with self._uow_factory() as uow:
            existing = self._find_existing(uow, event.event_id, subscription_id, dedupe_key)
            if existing is not None and existing.status not in ("pending", "aggregated"):
                return None
            records = uow.event_deliveries.list_by_subscription(
                event.project_id or "",
                subscription_id,
                status=_AGGREGATED_STATUS,
                aggregation_key=aggregation_key,
                limit=policy.max_items,
            )
            if existing is None:
                delivery = uow.event_deliveries.add(
                    self._new_delivery(
                        event,
                        subscription_id,
                        endpoint_id,
                        dedupe_key=dedupe_key,
                        aggregation_key=aggregation_key,
                    )
                )
            else:
                delivery = existing
            events = [_event_content(record) for record in records]
            events.append(delivery.content)
            events = events[-policy.max_items :]
            delivery.content = {
                "event_type": "run.summary",
                "event_id": event.event_id,
                "task_id": event.payload.get("task_id"),
                "aggregate_id": event.aggregate_id,
                "project_id": event.project_id,
                "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
                "events": events,
            }
            delivery.status = "pending"
            delivery.aggregation_key = aggregation_key
            delivery.item_count = len(events)
            if delivery.id is not None:
                delivery = uow.event_deliveries.update(delivery)
            for record in records:
                if record.id != delivery.id:
                    record.status = _COALESCED_STATUS
                    uow.event_deliveries.update(record)
        message = NotificationMessage(
            subject="[AETP] run.summary",
            body=_summary_body(delivery.content),
            severity="info" if event.payload.get("passed") else "error",
            event=event,
        )
        return delivery, message

    @staticmethod
    def _find_existing(uow, event_id: str, subscription_id: str, dedupe_key: str) -> EventDelivery | None:
        if dedupe_key == event_id:
            return uow.event_deliveries.get_by_event_subscription(event_id, subscription_id)
        return uow.event_deliveries.get_by_event_subscription_dedupe(event_id, subscription_id, dedupe_key)

    @staticmethod
    def _new_delivery(
        event: DomainEvent,
        subscription_id: str,
        endpoint_id: str,
        *,
        dedupe_key: str,
        status: str = "pending",
        aggregation_key: str | None = None,
        window_ends_at: datetime | None = None,
    ) -> EventDelivery:
        return EventDelivery(
            delivery_id=new_id(),
            project_id=event.project_id or "",
            event_id=event.event_id,
            subscription_id=subscription_id,
            endpoint_id=endpoint_id,
            dedupe_key=dedupe_key,
            aggregation_key=aggregation_key,
            window_ends_at=window_ends_at,
            content=_event_content(event),
            status=status,
        )

    @staticmethod
    def _matches_filter(event: DomainEvent, filters: dict) -> bool:
        """兼容旧订阅的 payload 顶层字段筛选。"""
        return all(event.payload.get(key) == expected for key, expected in (filters or {}).items())

    async def flush_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        """发送已到窗口的 Digest 聚合，返回成功投递数。"""
        current = now or utcnow()
        with self._uow_factory() as uow:
            due = uow.event_deliveries.list_due_aggregates(current, limit=limit)
        delivered = 0
        for seed in due:
            with self._uow_factory() as uow:
                subscription = uow.event_subscriptions.get_by_subscription_id(seed.subscription_id)
                if subscription is None or not subscription.enabled:
                    continue
                try:
                    policy = NotificationPolicy.model_validate(subscription.throttle_policy or {})
                except ValueError:
                    continue
                records = uow.event_deliveries.list_by_subscription(
                    seed.project_id,
                    seed.subscription_id,
                    status=_AGGREGATED_STATUS,
                    aggregation_key=seed.aggregation_key,
                    limit=policy.max_items,
                )
                if not records:
                    continue
                event = _event_from_content(records[0].content)
                content = {
                    "event_type": "notification.digest",
                    "event_id": records[0].event_id,
                    "project_id": seed.project_id,
                    "events": [record.content for record in records[-policy.max_items :]],
                }
                message = NotificationMessage(
                    subject="[AETP] notification.digest",
                    body=_summary_body(content),
                    severity="info",
                    event=event,
                )
            try:
                result = await self._deliver(event, seed.endpoint_id, message=message)
                status = result.status
                error_message = None if status == "succeeded" else result.detail
            except Exception as exc:  # noqa: BLE001 - 单个 Digest 失败不阻塞其他窗口
                logger.exception("Digest 投递失败: subscription=%s", seed.subscription_id)
                status = "failed"
                error_message = str(exc)[:500]
            with self._uow_factory() as uow:
                records = uow.event_deliveries.list_by_subscription(
                    seed.project_id,
                    seed.subscription_id,
                    status=_AGGREGATED_STATUS,
                    aggregation_key=seed.aggregation_key,
                    limit=policy.max_items,
                )
                if not records:
                    continue
                first = records[0]
                first.status = status
                first.attempts += 1
                first.sent_at = utcnow() if status == "succeeded" else None
                first.error_message = error_message
                first.content = content
                first.item_count = len(records)
                uow.event_deliveries.update(first)
                for record in records[1:]:
                    record.status = _COALESCED_STATUS
                    uow.event_deliveries.update(record)
            if status == "succeeded":
                delivered += 1
        return delivered

    async def _deliver(
        self,
        event: DomainEvent,
        endpoint_id: str,
        *,
        message: NotificationMessage | None = None,
    ):
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

        message = message or _message_for_event(event)

        secret_value = self._get_secret(ep_model.secret_ref) if ep_model.secret_ref else None
        return await sender.send(message, ep, secret_value=secret_value)


def _event_content(event: DomainEvent | EventDelivery) -> dict:
    if isinstance(event, EventDelivery):
        return dict(event.content)
    return {
        "event_type": event.event_type,
        "event_id": event.event_id,
        "task_id": event.payload.get("task_id"),
        "aggregate_id": event.aggregate_id,
        "project_id": event.project_id,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "payload": event.payload,
    }


def _event_from_content(content: dict) -> DomainEvent:
    payload = content.get("payload")
    return DomainEvent(
        event_id=str(content.get("event_id") or new_id()),
        project_id=content.get("project_id"),
        event_type=str(content.get("event_type") or "notification.digest"),
        aggregate_id=str(content.get("aggregate_id") or content.get("event_id") or "digest"),
        payload=dict(payload) if isinstance(payload, dict) else {},
    )


def _message_for_event(event: DomainEvent) -> NotificationMessage:
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
    return NotificationMessage(
        subject=f"[AETP] {event.event_type}",
        body=body,
        severity=severity,
        event=event,
    )


def _summary_body(content: dict) -> str:
    events = content.get("events")
    if not isinstance(events, list):
        return str(content)
    lines = [f"通知摘要（{len(events)} 条）"]
    for item in events:
        if isinstance(item, dict):
            lines.append(f"- {item.get('event_type', 'event')}: {item.get('task_id') or item.get('event_id', '')}")
    return "\n".join(lines)


def _dedupe_key(policy: NotificationPolicy, event: DomainEvent) -> str:
    if policy.dedupe_key is None:
        return event.event_id
    values = {**event.payload, "event_id": event.event_id, "event_type": event.event_type}
    try:
        value = policy.dedupe_key.format_map(_FormatValues(values))
    except (KeyError, ValueError):
        value = policy.dedupe_key
    return value[:255]


class _FormatValues(dict):
    def __missing__(self, key):
        return ""


def _run_aggregation_key(event: DomainEvent) -> str:
    return f"run:{event.payload.get('run_id') or event.aggregate_id}"[:255]


def _aggregation_key(subscription_id: str, occurred_at: datetime, window_s: int) -> str:
    start = int(occurred_at.timestamp()) // window_s * window_s
    return f"digest:{subscription_id}:{start}"[:255]


def _window_ends_at(occurred_at: datetime, window_s: int) -> datetime:
    start = int(occurred_at.timestamp()) // window_s * window_s
    return datetime.fromtimestamp(start + window_s, tz=UTC)
