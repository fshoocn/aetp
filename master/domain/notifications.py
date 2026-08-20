"""通知端口（P3.7，§10.4/§10.5，D-13）。

通知、外部回调与生命周期扩展统一基于持久化领域事件 + 通知端口实现；
核心业务代码不得直接依赖任何具体平台 SDK（email/webhook/飞书等）。

安全约定（§10.5）：
- 端点只存 secret_ref，不存明文密钥；SecretStore 负责取回
- 密钥值不落日志、SSE、审计
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from master.domain.models import DomainEvent


@dataclass(frozen=True)
class NotificationEndpoint:
    """通知端点（配置 + 密钥引用，不含明文密钥）。"""

    # sym:endpoint_id 端点业务标识
    endpoint_id: str
    # sym:channel_type 通道类型（email/generic_webhook/feishu/dingtalk/slack/teams/console_test）
    channel_type: str
    # sym:name 端点显示名
    name: str
    # sym:config 通道配置（脱敏，不含密钥）
    config: Mapping[str, Any] = field(default_factory=dict)
    # sym:secret_ref 密钥引用（SecretStore.get 取回，不存明文）
    secret_ref: str | None = None


@dataclass(frozen=True)
class NotificationMessage:
    """通知消息（模板只访问白名单字段，渲染由 sender adapter 完成）。"""

    # sym:subject 标题
    subject: str
    # sym:body 正文（Markdown/HTML 由 sender 渲染）
    body: str
    # sym:severity 严重级别（info/warn/error）
    severity: str = "info"
    # sym:event 关联领域事件（用于幂等 event_id、上下文）
    event: DomainEvent | None = None


@dataclass(frozen=True)
class DeliveryReceipt:
    """投递结果（sender 返回）。"""

    # sym:status 投递状态（succeeded/failed/retrying）
    status: str
    # sym:detail 结果说明（脱敏）
    detail: str = ""


@dataclass(frozen=True)
class SecretValue:
    """密钥值（不得落日志/SSE/审计，§10.5）。"""

    value: str


class NotificationSender(ABC):
    """通知发送器端口：业务层不依赖具体平台 SDK（D-13）。

    所有 sender adapter 必须继承此类并实现 send()。
    channel_type 与 notification_endpoints.channel_type 对应。
    """

    # sym:channel_type 该 sender 支持的通道类型
    channel_type: str

    @abstractmethod
    async def send(
        self,
        message: NotificationMessage,
        endpoint: NotificationEndpoint,
        *,
        secret_value: str | None = None,
    ) -> DeliveryReceipt:
        """发送一条通知；超时必须设上限，不得无限等待（§10.5）。

        Args:
            secret_value: 端点密钥明文（由 dispatcher 从 SecretStore 解回），
                          大部分 sender 忽略即可，仅 HMAC 签名类 sender 使用。
        """
        ...


class SecretStore(ABC):
    """密钥存储端口：业务层只持有 secret_ref。"""

    @abstractmethod
    def get(self, secret_ref: str) -> SecretValue:
        """按引用取回密钥值。"""
        ...
