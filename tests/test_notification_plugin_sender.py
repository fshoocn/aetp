"""PluginNotificationSender（notifier 插件 → 内部 sender 桥接）测试。"""

from __future__ import annotations

import asyncio

from aetp_protocol.reporting import DeliveryResult, NotificationDelivery

from master.adapters.notifications.plugin_sender import PluginNotificationSender
from master.domain.models import DomainEvent
from master.domain.notifications import NotificationEndpoint, NotificationMessage
from master.domain.time import utcnow


def _message() -> NotificationMessage:
    event = DomainEvent(
        event_id="EVT-PLUGIN-1",
        project_id="PRJ-1",
        event_type="run.completed",
        aggregate_id="RUN-1",
        payload={"run_id": "RUN-1"},
        occurred_at=utcnow(),
    )
    return NotificationMessage(
        subject="[AETP] run.completed",
        body="测试消息",
        severity="info",
        event=event,
    )


def _endpoint() -> NotificationEndpoint:
    return NotificationEndpoint(
        endpoint_id="EP-PLUGIN-1",
        channel_type="org.plugin.channel",
        name="plugin channel",
        config={"url": "https://example.invalid/hook"},
        secret_ref="ref/1",
    )


class _FakeChannel:
    channel_type = "org.plugin.channel"

    def __init__(self) -> None:
        self.captured: list[tuple[NotificationDelivery, str | None]] = []

    async def deliver(self, delivery, secret_value=None) -> DeliveryResult:
        self.captured.append((delivery, secret_value))
        return DeliveryResult(status="succeeded")


def test_adapter_bridges_to_plugin_deliver() -> None:
    channel = _FakeChannel()
    sender = PluginNotificationSender(channel)

    assert sender.channel_type == "org.plugin.channel"
    result = asyncio.run(sender.send(_message(), _endpoint(), secret_value="secret"))

    assert result.status == "succeeded"
    assert len(channel.captured) == 1
    delivery, secret = channel.captured[0]
    assert secret == "secret"
    assert delivery.channel_type == "org.plugin.channel"
    assert delivery.subject == "[AETP] run.completed"
    assert delivery.body == "测试消息"
    assert delivery.severity == "info"
    assert delivery.endpoint_config == {"url": "https://example.invalid/hook"}
    assert delivery.event_id == "EVT-PLUGIN-1"
    assert delivery.event_type == "run.completed"
    assert delivery.project_id == "PRJ-1"
    assert delivery.payload == {"run_id": "RUN-1"}


def test_adapter_maps_failed_delivery() -> None:
    class _Fail:
        channel_type = "org.plugin.channel"

        async def deliver(self, delivery, secret_value=None) -> DeliveryResult:
            return DeliveryResult(status="failed", error="boom")

    sender = PluginNotificationSender(_Fail())
    result = asyncio.run(sender.send(_message(), _endpoint()))
    assert result.status == "failed"
    assert result.detail == "boom"


def test_adapter_requires_channel_type_and_deliver() -> None:
    class _NoType:
        async def deliver(self, delivery, secret_value=None) -> DeliveryResult:
            return DeliveryResult(status="succeeded")

    try:
        PluginNotificationSender(_NoType())
    except ValueError as exc:
        assert "channel_type" in str(exc)
    else:
        raise AssertionError("缺 channel_type 应报错")

    class _NoDeliver:
        channel_type = "org.plugin.channel"

    try:
        PluginNotificationSender(_NoDeliver())
    except ValueError as exc:
        assert "deliver" in str(exc)
    else:
        raise AssertionError("缺 deliver 应报错")
