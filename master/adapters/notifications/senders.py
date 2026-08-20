"""通知 Sender Adapters（P8.5，§10.5）。

实现 NotificationSender 端口的具体投递器，每种 channel_type 一个 adapter。
全部 sender 继承 NotificationSender ABC，HTTP 类 sender 复用 HttpSender 基类。
SenderRegistry 提供运行时注册/查询能力，支持插件扩展。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from master.domain.notifications import (
    DeliveryReceipt,
    NotificationEndpoint,
    NotificationMessage,
    NotificationSender,
)

logger = logging.getLogger(__name__)

# ── HTTP 基类 ────────────────────────────────────────────────────────────


class HttpSender(NotificationSender):
    """HTTP 类 sender 共用基类：httpx 异步客户端 + 统一超时/错误处理。"""

    # 子类可覆盖
    _timeout: float = 10.0

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> DeliveryReceipt:
        """POST JSON 并返回 DeliveryReceipt，统一异常处理。"""
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=hdrs)
            return DeliveryReceipt(
                status="succeeded" if resp.is_success else "failed",
                detail=f"HTTP {resp.status_code}",
            )
        except httpx.HTTPError as exc:
            return DeliveryReceipt(status="failed", detail=str(exc)[:256])


# ── 具体 Sender ─────────────────────────────────────────────────────────


class ConsoleSender(NotificationSender):
    """控制台测试 sender（本地开发用）。"""

    channel_type = "console_test"

    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        logger.info(
            "[通知] %s | 端点=%s | 严重级别=%s\n%s",
            message.subject,
            endpoint.name,
            message.severity,
            message.body[:500],
        )
        return DeliveryReceipt(status="succeeded", detail="控制台输出完成")


class GenericWebhookSender(HttpSender):
    """通用 Webhook sender（出站事件 Hook，§10.5）。"""

    channel_type = "generic_webhook"

    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        url = endpoint.config.get("url")
        if not url or not isinstance(url, str):
            return DeliveryReceipt(status="failed", detail="缺少 webhook url")

        payload: dict[str, Any] = {
            "subject": message.subject,
            "body": message.body,
            "severity": message.severity,
            "event": None,
        }
        if message.event is not None:
            payload["event"] = {
                "event_id": message.event.event_id,
                "event_type": message.event.event_type,
                "project_id": message.event.project_id,
                "aggregate_id": message.event.aggregate_id,
                "occurred_at": (
                    message.event.occurred_at.isoformat()
                    if message.event.occurred_at
                    else None
                ),
                "payload": message.event.payload,
            }

        extra_headers: dict[str, str] = {
            "X-AETP-Event-Id": message.event.event_id if message.event else "",
        }

        if secret_value and message.event:
            timestamp = (
                message.event.occurred_at.isoformat()
                if message.event.occurred_at
                else ""
            )
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            signature = hmac.new(
                secret_value.encode("utf-8"),
                f"{timestamp}.{body_bytes.decode('utf-8')}".encode(),
                hashlib.sha256,
            ).hexdigest()
            extra_headers["X-AETP-Timestamp"] = timestamp
            extra_headers["X-AETP-Signature"] = f"sha256={signature}"

        return await self._post_json(url, payload, extra_headers)


class FeishuSender(HttpSender):
    """飞书机器人 sender。"""

    channel_type = "feishu"

    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        url = endpoint.config.get("webhook_url") or endpoint.config.get("url")
        if not url or not isinstance(url, str):
            return DeliveryReceipt(status="failed", detail="缺少飞书 webhook URL")

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": message.subject},
                    "template": (
                        "red"
                        if message.severity == "error"
                        else ("orange" if message.severity == "warn" else "blue")
                    ),
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": message.body[:1000],
                        },
                    }
                ],
            },
        }
        return await self._post_json(url, payload)


class DingtalkSender(HttpSender):
    """钉钉机器人 sender。"""

    channel_type = "dingtalk"

    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        url = endpoint.config.get("webhook_url") or endpoint.config.get("url")
        if not url or not isinstance(url, str):
            return DeliveryReceipt(status="failed", detail="缺少钉钉 webhook URL")

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": message.subject,
                "text": f"### {message.subject}\n\n{message.body[:1000]}",
            },
        }
        return await self._post_json(url, payload)


class SlackSender(HttpSender):
    """Slack Incoming Webhook sender。"""

    channel_type = "slack"

    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        url = endpoint.config.get("webhook_url") or endpoint.config.get("url")
        if not url or not isinstance(url, str):
            return DeliveryReceipt(status="failed", detail="缺少 Slack webhook URL")

        payload = {
            "text": f"*{message.subject}*\n{message.body[:1000]}",
            "unfurl_links": False,
        }
        return await self._post_json(url, payload)


class TeamsSender(HttpSender):
    """Microsoft Teams Incoming Webhook sender。"""

    channel_type = "teams"

    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        url = endpoint.config.get("webhook_url") or endpoint.config.get("url")
        if not url or not isinstance(url, str):
            return DeliveryReceipt(status="failed", detail="缺少 Teams webhook URL")

        payload = {
            "@type": "MessageCard",
            "summary": message.subject,
            "sections": [
                {
                    "activityTitle": message.subject,
                    "text": message.body[:1000],
                    "markdown": True,
                }
            ],
        }
        return await self._post_json(url, payload)


class EmailSender(NotificationSender):
    """邮件 sender（占位，需 SMTP 配置）。"""

    channel_type = "email"

    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        logger.info(
            "[邮件通知] to=%s subject=%s body=%s",
            endpoint.config.get("to"),
            message.subject,
            message.body[:200],
        )
        return DeliveryReceipt(status="succeeded", detail="占位实现，已记录日志")


# ── SenderRegistry ───────────────────────────────────────────────────────


class SenderRegistry:
    """Sender 注册中心：运行时注册/查询 sender adapter。

    支持插件通过 register() 注册自定义 sender，无需修改核心代码。
    """

    def __init__(self) -> None:
        self._senders: dict[str, NotificationSender] = {}

    def register(self, sender: NotificationSender) -> None:
        """注册一个 sender，已存在同 channel_type 则覆盖。"""
        self._senders[sender.channel_type] = sender
        logger.info("注册通知 sender: channel_type=%s", sender.channel_type)

    def get(self, channel_type: str) -> NotificationSender | None:
        """按 channel_type 获取 sender。"""
        return self._senders.get(channel_type)

    def all(self) -> dict[str, NotificationSender]:
        """返回所有已注册 sender 的快照。"""
        return dict(self._senders)

    def channel_types(self) -> list[str]:
        """返回所有已注册的 channel_type。"""
        return list(self._senders.keys())


def build_default_registry() -> SenderRegistry:
    """构建包含所有内置 sender 的注册中心。"""
    registry = SenderRegistry()
    for cls in [
        ConsoleSender,
        GenericWebhookSender,
        FeishuSender,
        DingtalkSender,
        SlackSender,
        TeamsSender,
        EmailSender,
    ]:
        registry.register(cls())
    return registry
