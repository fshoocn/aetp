"""通知管理服务（P7.6，§10.5）。

项目范围通知端点、事件订阅、投递状态的 CRUD 与重试。
密钥通过 SecretStore 间接管理，API/日志/审计永不回显明文。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aetp_protocol.ids import new_id

from master.domain.models.notification import (
    EventDelivery,
    EventSubscription,
    NotificationEndpoint,
)
from master.domain.notifications import SecretStore
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)


class NotificationService:
    """通知端点、事件订阅、投递记录管理。"""

    VALID_CHANNEL_TYPES = frozenset(
        {
            "email",
            "generic_webhook",
            "feishu",
            "dingtalk",
            "slack",
            "teams",
            "console_test",
        }
    )
    MAX_RETRY_ATTEMPTS = 10

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._store = secret_store

    # -- 通知端点 CRUD --------------------------------------------------------

    def create_endpoint(
        self,
        *,
        project_id: str,
        channel_type: str,
        name: str,
        config: dict | None = None,
        secret_value: str | None = None,
        created_by: int,
    ) -> NotificationEndpoint:
        if channel_type not in self.VALID_CHANNEL_TYPES:
            raise ValueError(f"不支持的通道类型: {channel_type}; 可选: {', '.join(sorted(self.VALID_CHANNEL_TYPES))}")
        if not name.strip():
            raise ValueError("端点名称不能为空")

        secret_ref = None
        if secret_value:
            secret_ref = new_id()
            self._store_secret(secret_ref, secret_value)

        with self._uow_factory() as uow:
            endpoint = NotificationEndpoint(
                endpoint_id=new_id(),
                project_id=project_id,
                channel_type=channel_type,
                name=name.strip(),
                config=config or {},
                secret_ref=secret_ref,
                enabled=True,
                created_by=created_by,
            )
            endpoint = uow.notification_endpoints.add(endpoint)
        logger.info("通知端点已创建: endpoint_id=%s channel=%s", endpoint.endpoint_id, channel_type)
        return endpoint

    def list_endpoints(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list[NotificationEndpoint]:
        with self._uow_factory() as uow:
            return uow.notification_endpoints.list_by_project(project_id, limit=limit, offset=offset)

    def get_endpoint(self, endpoint_id: str, project_id: str) -> NotificationEndpoint | None:
        with self._uow_factory() as uow:
            ep = uow.notification_endpoints.get_by_endpoint_id(endpoint_id)
            if ep is None or ep.project_id != project_id:
                return None
            return ep

    def update_endpoint(
        self,
        endpoint_id: str,
        *,
        project_id: str,
        name: str | None = None,
        config: dict | None = None,
        secret_value: str | None = None,
        enabled: bool | None = None,
    ) -> NotificationEndpoint:
        with self._uow_factory() as uow:
            ep = uow.notification_endpoints.get_by_endpoint_id(endpoint_id)
            if ep is None or ep.project_id != project_id:
                raise ValueError(f"通知端点不存在: {endpoint_id}")
            if name is not None:
                ep.name = name.strip()
            if config is not None:
                ep.config = config
            if enabled is not None:
                ep.enabled = enabled
            if secret_value is not None:
                if ep.secret_ref is None:
                    ep.secret_ref = new_id()
                self._store_secret(ep.secret_ref, secret_value)
            return uow.notification_endpoints.update(ep)

    def delete_endpoint(self, endpoint_id: str, project_id: str) -> None:
        with self._uow_factory() as uow:
            ep = uow.notification_endpoints.get_by_endpoint_id(endpoint_id)
            if ep is None or ep.project_id != project_id:
                raise ValueError(f"通知端点不存在: {endpoint_id}")
            # 删除端点时级联删除关联订阅（ORM CASCADE），清理密钥
            if ep.secret_ref:
                self._delete_secret(ep.secret_ref)
            uow.notification_endpoints.delete(endpoint_id)
        logger.info("通知端点已删除: endpoint_id=%s", endpoint_id)

    # -- 事件订阅 CRUD --------------------------------------------------------

    def create_subscription(
        self,
        *,
        project_id: str,
        endpoint_id: str,
        task_id: str | None = None,
        event_types: list[str],
        filter_json: dict | None = None,
        throttle_policy: dict | None = None,
        created_by: int,
    ) -> EventSubscription:
        if not event_types:
            raise ValueError("event_types 不能为空")
        with self._uow_factory() as uow:
            ep = uow.notification_endpoints.get_by_endpoint_id(endpoint_id)
            if ep is None or ep.project_id != project_id:
                raise ValueError(f"通知端点不存在: {endpoint_id}")
            normalized_task_id = task_id.strip() if task_id and task_id.strip() else None
            if normalized_task_id and uow.test_tasks.get_by_task_id(normalized_task_id, project_id) is None:
                raise ValueError(f"测试任务不存在或不属于当前项目: {normalized_task_id}")
            sub = EventSubscription(
                subscription_id=new_id(),
                project_id=project_id,
                endpoint_id=endpoint_id,
                task_id=normalized_task_id,
                event_types=event_types,
                filter_json=filter_json or {},
                throttle_policy=throttle_policy or {},
                enabled=True,
                created_by=created_by,
            )
            sub = uow.event_subscriptions.add(sub)
        logger.info(
            "事件订阅已创建: subscription_id=%s endpoint=%s events=%s",
            sub.subscription_id,
            endpoint_id,
            event_types,
        )
        return sub

    def list_subscriptions(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list[EventSubscription]:
        with self._uow_factory() as uow:
            return uow.event_subscriptions.list_by_project(project_id, limit=limit, offset=offset)

    def update_subscription(
        self,
        subscription_id: str,
        *,
        project_id: str,
        event_types: list[str] | None = None,
        task_id: str | None = None,
        filter_json: dict | None = None,
        throttle_policy: dict | None = None,
        enabled: bool | None = None,
    ) -> EventSubscription:
        with self._uow_factory() as uow:
            sub = uow.event_subscriptions.get_by_subscription_id(subscription_id)
            if sub is None or sub.project_id != project_id:
                raise ValueError(f"事件订阅不存在: {subscription_id}")
            if event_types is not None:
                sub.event_types = event_types
            if task_id is not None:
                normalized_task_id = task_id.strip() if task_id.strip() else None
                if normalized_task_id and uow.test_tasks.get_by_task_id(normalized_task_id, project_id) is None:
                    raise ValueError(f"测试任务不存在或不属于当前项目: {normalized_task_id}")
                sub.task_id = normalized_task_id
            if filter_json is not None:
                sub.filter_json = filter_json
            if throttle_policy is not None:
                sub.throttle_policy = throttle_policy
            if enabled is not None:
                sub.enabled = enabled
            return uow.event_subscriptions.update(sub)

    def delete_subscription(self, subscription_id: str, project_id: str) -> None:
        with self._uow_factory() as uow:
            sub = uow.event_subscriptions.get_by_subscription_id(subscription_id)
            if sub is None or sub.project_id != project_id:
                raise ValueError(f"事件订阅不存在: {subscription_id}")
            uow.event_subscriptions.delete(subscription_id)
        logger.info("事件订阅已删除: subscription_id=%s", subscription_id)

    # -- 投递状态 --------------------------------------------------------------

    def list_deliveries(
        self,
        project_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EventDelivery]:
        with self._uow_factory() as uow:
            deliveries = uow.event_deliveries.list_by_project(project_id, status=status, limit=limit, offset=offset)
            return [self._restore_delivery_content(uow, delivery) for delivery in deliveries]

    def get_delivery(self, delivery_id: str, project_id: str) -> EventDelivery | None:
        with self._uow_factory() as uow:
            d = uow.event_deliveries.get_by_delivery_id(delivery_id)
            if d is None or d.project_id != project_id:
                return None
            return self._restore_delivery_content(uow, d)

    @staticmethod
    def _restore_delivery_content(uow, delivery: EventDelivery) -> EventDelivery:
        """兼容旧记录：投递正文为空时从不可变领域事件恢复实际载荷。"""
        if delivery.content:
            return delivery
        event = uow.domain_events.get_by_event_id(delivery.event_id)
        if event is None:
            return delivery
        delivery.content = {
            "event_type": event.event_type,
            "event_id": event.event_id,
            "task_id": event.payload.get("task_id"),
            "aggregate_id": event.aggregate_id,
            "project_id": event.project_id,
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
            "payload": event.payload,
        }
        return delivery

    def retry_delivery(self, delivery_id: str, project_id: str) -> EventDelivery:
        with self._uow_factory() as uow:
            d = uow.event_deliveries.get_by_delivery_id(delivery_id)
            if d is None or d.project_id != project_id:
                raise ValueError(f"投递记录不存在: {delivery_id}")
            if d.status not in ("failed", "exhausted"):
                raise ValueError(f"当前状态 {d.status} 不允许重试")
            if d.attempts >= self.MAX_RETRY_ATTEMPTS:
                raise ValueError("已达到最大重试次数")
            d.status = "pending"
            d.next_attempt_at = utcnow()
            return uow.event_deliveries.update(d)

    # -- 密钥查询（内部，不暴露给 API） ----------------------------------------

    def get_secret(self, secret_ref: str) -> str | None:
        """从 SecretStore 解回密钥明文（不存在/解密失败返回 None）。"""
        if self._store is None:
            return None
        value = self._store.get(secret_ref)
        return value.value if value is not None else None

    def _store_secret(self, secret_ref: str, value: str) -> None:
        """加密并持久化密钥；无 SecretStore 时静默跳过（开发兜底）。"""
        if self._store is None:
            logger.warning("未配置 SecretStore，密钥未持久化: %s", secret_ref)
            return
        self._store.set(secret_ref, value)

    def _delete_secret(self, secret_ref: str) -> None:
        if self._store is None:
            return
        self._store.delete(secret_ref)
