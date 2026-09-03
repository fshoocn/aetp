"""当前 HTTP API 的共享请求与响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from master.domain.enums import AccountStatus, PlatformRole


class AdminUserOut(BaseModel):
    """管理员视角的用户信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    account_status: AccountStatus
    platform_role: PlatformRole
    created_at: datetime


class UserApprovalRequest(BaseModel):
    """管理员审批或修改用户属性。"""

    account_status: AccountStatus | None = None
    platform_role: PlatformRole | None = None


class EndpointCreate(BaseModel):
    """创建通知端点请求。"""

    channel_type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    config: dict = Field(default_factory=dict)
    secret_value: str | None = Field(default=None, max_length=2048)


class EndpointUpdate(BaseModel):
    """更新通知端点请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    config: dict | None = None
    secret_value: str | None = Field(default=None, max_length=2048)
    enabled: bool | None = None


class EndpointOut(BaseModel):
    """通知端点响应，不包含密钥。"""

    model_config = ConfigDict(from_attributes=True)

    endpoint_id: str
    project_id: str
    channel_type: str
    name: str
    config: dict = Field(default_factory=dict)
    has_secret: bool = False
    enabled: bool = True
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _compute_has_secret(cls, data: object) -> object:
        if hasattr(data, "secret_ref"):
            object.__setattr__(data, "has_secret", bool(data.secret_ref))  # type: ignore[attr-defined]
        elif isinstance(data, dict) and "has_secret" not in data:
            data["has_secret"] = bool(data.get("secret_ref"))
        return data


class SubscriptionCreate(BaseModel):
    """创建事件订阅请求。"""

    endpoint_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    event_types: list[str] = Field(min_length=1)
    filter_json: dict = Field(default_factory=dict)
    throttle_policy: dict = Field(default_factory=dict)


class SubscriptionUpdate(BaseModel):
    """更新事件订阅请求。"""

    event_types: list[str] | None = None
    task_id: str | None = Field(default=None, max_length=64)
    filter_json: dict | None = None
    throttle_policy: dict | None = None
    enabled: bool | None = None


class SubscriptionOut(BaseModel):
    """事件订阅响应。"""

    model_config = ConfigDict(from_attributes=True)

    subscription_id: str
    project_id: str
    endpoint_id: str
    task_id: str | None = None
    event_types: list[str] = Field(default_factory=list)
    filter_json: dict = Field(default_factory=dict)
    throttle_policy: dict = Field(default_factory=dict)
    enabled: bool = True
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeliveryOut(BaseModel):
    """通知投递记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    delivery_id: str
    project_id: str
    event_id: str
    subscription_id: str
    endpoint_id: str
    content: dict = Field(default_factory=dict)
    status: str
    attempts: int = 0
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    response_summary: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
