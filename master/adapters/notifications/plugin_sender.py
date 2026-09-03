"""通知渠道插件 → 内部 NotificationSender 的适配器。

插件实现 ``aetp_protocol.reporting.NotifierChannel``（channel_type + deliver），
本适配器把它包装成内部 ``master.domain.notifications.NotificationSender``
（send(message, endpoint, secret_value) -> DeliveryReceipt），供
``NotificationDispatcher`` 使用。适配器只做 DTO 转换，不含业务逻辑。
"""

from __future__ import annotations

from aetp_protocol.ids import JsonObject
from aetp_protocol.reporting import DeliveryResult, NotificationDelivery

from master.domain.notifications import (
    DeliveryReceipt,
    NotificationEndpoint,
    NotificationMessage,
    NotificationSender,
)


class PluginNotificationSender(NotificationSender):
    """把协议 NotifierChannel 桥接成内部 NotificationSender。"""

    def __init__(self, plugin) -> None:
        self.channel_type = str(getattr(plugin, "channel_type", "")).strip()
        if not self.channel_type:
            raise ValueError("notifier 插件缺少 channel_type")
        self._deliver = getattr(plugin, "deliver", None)
        if not callable(self._deliver):
            raise ValueError(f"notifier 插件缺少 deliver(): {self.channel_type}")

    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        event = message.event
        result = await self._deliver(
            NotificationDelivery(
                channel_type=self.channel_type,
                subject=message.subject,
                body=message.body,
                severity=message.severity,
                endpoint_config=_json_object(endpoint.config),
                event_id=event.event_id if event is not None else None,
                event_type=event.event_type if event is not None else None,
                project_id=event.project_id if event is not None else None,
                payload=_json_object(event.payload) if event is not None else {},
            ),
            secret_value=secret_value,
        )
        if not isinstance(result, DeliveryResult):
            raise TypeError("notifier 插件 deliver() 必须返回 DeliveryResult")
        return DeliveryReceipt(
            status=result.status,
            detail=result.error or "",
        )


def _json_object(value: object) -> JsonObject:
    """尽力把端点配置/事件载荷转成 JsonObject（跳过非 JSON 值）。"""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items() if _is_json(item)}
    return {}


def _is_json(value: object) -> bool:
    return value is None or isinstance(value, (bool, int, float, str, list, dict))


__all__ = ["PluginNotificationSender"]
