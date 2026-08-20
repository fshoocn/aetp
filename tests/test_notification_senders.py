"""P8.5 通知 Sender Adapters 测试。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from master.adapters.notifications.senders import (
    ConsoleSender,
    GenericWebhookSender,
    FeishuSender,
    DingtalkSender,
    SlackSender,
    TeamsSender,
    EmailSender,
    SenderRegistry,
    build_default_registry,
)
from master.domain.notifications import (
    DeliveryReceipt,
    NotificationEndpoint,
    NotificationMessage,
    NotificationSender,
)
from master.domain.models import DomainEvent
from master.application.services.notification_dispatcher import NotificationDispatcher
from master.domain.models.notification import EventDelivery, EventSubscription, NotificationEndpoint as ModelEndpoint
from master.domain.time import utcnow


# ── Fixtures ──────────────────────────────────────────────────────────────

def _protocol_endpoint(channel_type: str, **overrides) -> NotificationEndpoint:
    """创建协议端点（senders 使用的 frozen dataclass）。"""
    defaults = {
        "endpoint_id": "EP-TEST",
        "channel_type": channel_type,
        "name": "test-endpoint",
        "config": {},
        "secret_ref": None,
    }
    defaults.update(overrides)
    return NotificationEndpoint(**defaults)


def _model_endpoint(channel_type: str, **overrides) -> ModelEndpoint:
    """创建领域模型端点（repository 返回的 mutable dataclass）。"""
    defaults = {
        "endpoint_id": "EP-TEST",
        "project_id": "PRJ-1",
        "channel_type": channel_type,
        "name": "test-endpoint",
        "config": {},
        "secret_ref": None,
        "enabled": True,
    }
    defaults.update(overrides)
    return ModelEndpoint(**defaults)


def _message() -> NotificationMessage:
    event = DomainEvent(
        event_id="EVT-1",
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


def _mock_httpx_response(status_code: int = 200) -> MagicMock:
    """创建 mock httpx.Response。"""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.is_success = 200 <= status_code < 300
    return mock_resp


def _mock_httpx_client(response: MagicMock | None = None) -> MagicMock:
    """创建 mock httpx.AsyncClient 上下文管理器。"""
    if response is None:
        response = _mock_httpx_response(200)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


# ── Sender 合约测试 ──────────────────────────────────────────────────────

class TestConsoleSender:
    def test_console_sender_returns_succeeded(self):
        """ConsoleSender 接受 message + endpoint 并返回 succeeded。"""
        sender = ConsoleSender()
        result = asyncio.run(sender.send(_message(), _protocol_endpoint("console_test")))
        assert result.status == "succeeded"
        assert result.detail is not None


class TestGenericWebhookSender:
    def test_generic_webhook_sender_sends_post(self):
        """GenericWebhookSender 使用 httpx POST 并支持 HMAC 签名。"""
        sender = GenericWebhookSender()
        ep = _protocol_endpoint(
            "generic_webhook",
            config={"url": "https://example.com/webhook"},
            secret_ref="wh-secret",
        )

        mock_client = _mock_httpx_client()
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(sender.send(_message(), ep, secret_value="test-secret"))

        assert result.status == "succeeded"
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://example.com/webhook"
        headers = call_args[1].get("headers", {})
        assert any(k.lower() == "x-aetp-signature" for k in headers)

    def test_generic_webhook_missing_url(self):
        """缺少 url 配置时返回 failed。"""
        sender = GenericWebhookSender()
        ep = _protocol_endpoint("generic_webhook", config={})
        result = asyncio.run(sender.send(_message(), ep))
        assert result.status == "failed"


class TestFeishuSender:
    def test_feishu_sender_posts_webhook(self):
        """FeishuSender 使用 httpx POST 飞书 webhook。"""
        sender = FeishuSender()
        ep = _protocol_endpoint(
            "feishu",
            config={"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test"},
        )

        mock_client = _mock_httpx_client()
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(sender.send(_message(), ep))

        assert result.status == "succeeded"

    def test_feishu_missing_url(self):
        """缺少 webhook_url 时返回 failed。"""
        sender = FeishuSender()
        ep = _protocol_endpoint("feishu", config={})
        result = asyncio.run(sender.send(_message(), ep))
        assert result.status == "failed"


class TestDingtalkSender:
    def test_dingtalk_sender_posts_webhook(self):
        """DingtalkSender 使用 httpx POST 钉钉 webhook。"""
        sender = DingtalkSender()
        ep = _protocol_endpoint(
            "dingtalk",
            config={"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=test"},
        )

        mock_client = _mock_httpx_client()
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(sender.send(_message(), ep))

        assert result.status == "succeeded"


class TestSlackSender:
    def test_slack_sender_posts_webhook(self):
        """SlackSender 使用 httpx POST Slack incoming webhook。"""
        sender = SlackSender()
        ep = _protocol_endpoint(
            "slack",
            config={"webhook_url": "https://hooks.slack.com/services/T/B/test"},
        )

        mock_client = _mock_httpx_client()
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(sender.send(_message(), ep))

        assert result.status == "succeeded"


class TestTeamsSender:
    def test_teams_sender_posts_connector(self):
        """TeamsSender 使用 httpx POST Teams Connector Card。"""
        sender = TeamsSender()
        ep = _protocol_endpoint(
            "teams",
            config={"webhook_url": "https://outlook.office.com/webhook/test"},
        )

        mock_client = _mock_httpx_client()
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(sender.send(_message(), ep))

        assert result.status == "succeeded"


class TestEmailSender:
    def test_email_sender_placeholder(self):
        """EmailSender 返回 succeeded（占位实现，记录日志）。"""
        sender = EmailSender()
        result = asyncio.run(sender.send(
            _message(),
            _protocol_endpoint("email", config={"to": "test@example.com"}),
        ))
        assert result.status == "succeeded"


# ── SenderRegistry 测试 ─────────────────────────────────────────────────

class TestSenderRegistry:
    def test_build_default_registry_returns_all(self):
        """build_default_registry() 返回包含所有 7 个 sender 的注册中心。"""
        registry = build_default_registry()
        types = registry.channel_types()
        assert "console_test" in types
        assert "generic_webhook" in types
        assert "feishu" in types
        assert "dingtalk" in types
        assert "slack" in types
        assert "teams" in types
        assert "email" in types
        assert len(types) == 7

    def test_sender_registry_register_custom(self):
        """SenderRegistry 支持运行时注册自定义 sender。"""
        registry = SenderRegistry()
        registry.register(ConsoleSender())
        assert registry.get("console_test") is not None
        assert registry.get("nonexistent") is None

    def test_sender_registry_inherits_abc(self):
        """所有内置 sender 均继承 NotificationSender ABC。"""
        registry = build_default_registry()
        for sender in registry.all().values():
            assert isinstance(sender, NotificationSender)


# ── Dispatcher 集成测试 ─────────────────────────────────────────────────

class TestNotificationDispatcher:
    def test_dispatcher_matches_event_type(self):
        """Dispatcher 只投递事件类型匹配的订阅。"""
        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        sub_match = EventSubscription(
            subscription_id="SUB-MATCH",
            project_id="PRJ-1",
            endpoint_id="EP-TEST",
            event_types=["run.completed"],
            enabled=True,
        )
        sub_mismatch = EventSubscription(
            subscription_id="SUB-MISMATCH",
            project_id="PRJ-1",
            endpoint_id="EP-OTHER",
            event_types=["run.failed"],
            enabled=True,
        )
        mock_uow.event_subscriptions.list_by_project.return_value = [sub_match, sub_mismatch]

        mock_ep = _model_endpoint("console_test", endpoint_id="EP-TEST")
        mock_uow.notification_endpoints.get_by_endpoint_id.return_value = mock_ep
        mock_uow.event_deliveries.get_by_event_subscription.return_value = None

        def uow_factory():
            return mock_uow

        dispatcher = NotificationDispatcher(
            uow_factory=uow_factory,
            registry=build_default_registry(),
        )
        event = DomainEvent(
            event_id="EVT-1",
            project_id="PRJ-1",
            event_type="run.completed",
            aggregate_id="RUN-1",
            payload={},
            occurred_at=utcnow(),
        )
        delivered = asyncio.run(dispatcher.dispatch(event))
        assert delivered == 1

    def test_dispatcher_skips_disabled_subscription(self):
        """Dispatcher 跳过已禁用的订阅。"""
        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        sub = EventSubscription(
            subscription_id="SUB-OFF",
            project_id="PRJ-1",
            endpoint_id="EP-TEST",
            event_types=["run.completed"],
            enabled=False,
        )
        mock_uow.event_subscriptions.list_by_project.return_value = [sub]
        mock_uow.event_deliveries.get_by_event_subscription.return_value = None

        def uow_factory():
            return mock_uow

        dispatcher = NotificationDispatcher(
            uow_factory=uow_factory,
            registry=build_default_registry(),
        )
        event = DomainEvent(
            event_id="EVT-2",
            project_id="PRJ-1",
            event_type="run.completed",
            aggregate_id="RUN-1",
            payload={},
            occurred_at=utcnow(),
        )
        delivered = asyncio.run(dispatcher.dispatch(event))
        assert delivered == 0

    def test_dispatcher_idempotent_delivery(self):
        """Dispatcher 幂等：已存在的 (event_id, subscription_id) 不重复投递。"""
        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        sub = EventSubscription(
            subscription_id="SUB-1",
            project_id="PRJ-1",
            endpoint_id="EP-TEST",
            event_types=["run.completed"],
            enabled=True,
        )
        mock_uow.event_subscriptions.list_by_project.return_value = [sub]
        mock_uow.event_deliveries.get_by_event_subscription.return_value = EventDelivery(
            delivery_id="DL-EXISTING",
            event_id="EVT-1",
            subscription_id="SUB-1",
        )

        def uow_factory():
            return mock_uow

        dispatcher = NotificationDispatcher(
            uow_factory=uow_factory,
            registry=build_default_registry(),
        )
        event = DomainEvent(
            event_id="EVT-1",
            project_id="PRJ-1",
            event_type="run.completed",
            aggregate_id="RUN-1",
            payload={},
            occurred_at=utcnow(),
        )
        delivered = asyncio.run(dispatcher.dispatch(event))
        assert delivered == 0
