"""Console notifier 渠道插件（独立插件包）。

把一次通知投递打印到 Master 控制台，用于调试/冒烟。实现
``aetp_protocol.reporting.NotifierChannel``（channel_type + deliver）。
"""

from __future__ import annotations

import logging

from aetp_protocol.reporting import DeliveryResult, NotificationDelivery

logger = logging.getLogger("aetp.notifier.console")


class ConsoleNotifier:
    channel_type = "console_test"

    async def deliver(
        self,
        delivery: NotificationDelivery,
        secret_value: str | None = None,
    ) -> DeliveryResult:
        del secret_value
        logger.info(
            "[console-notifier] %s | %s | %s\n%s",
            delivery.channel_type,
            delivery.severity,
            delivery.subject,
            delivery.body,
        )
        return DeliveryResult(status="succeeded")


def create_notifier() -> ConsoleNotifier:
    return ConsoleNotifier()


__all__ = ["ConsoleNotifier", "create_notifier"]
