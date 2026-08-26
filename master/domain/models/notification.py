"""领域对象：通知端点 / 事件订阅 / 投递记录（P7.6，§10.5）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NotificationEndpoint:
    """项目通知端点（配置 + 密钥引用，不含明文密钥）。"""

    id: int | None = None
    endpoint_id: str = ""
    project_id: str = ""
    channel_type: str = ""
    name: str = ""
    config: dict = field(default_factory=dict)
    secret_ref: str | None = None
    enabled: bool = True
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class EventSubscription:
    """项目事件订阅。"""

    id: int | None = None
    subscription_id: str = ""
    project_id: str = ""
    endpoint_id: str = ""
    task_id: str | None = None
    event_types: list[str] = field(default_factory=list)
    filter_json: dict = field(default_factory=dict)
    throttle_policy: dict = field(default_factory=dict)
    enabled: bool = True
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class EventDelivery:
    """投递记录。"""

    id: int | None = None
    delivery_id: str = ""
    project_id: str = ""
    event_id: str = ""
    subscription_id: str = ""
    endpoint_id: str = ""
    content: dict = field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    response_summary: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
