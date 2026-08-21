"""ORM：事件订阅。

项目 maintainer/owner 管理事件订阅规则，把领域事件绑定到通知端点。
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin


class EventSubscription(Base, TimestampMixin):
    __tablename__ = "event_subscriptions"
    __table_args__ = (Index("ix_event_subscriptions_project_enabled", "project_pk", "enabled"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    project_pk: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_pk: Mapped[int] = mapped_column(
        ForeignKey("notification_endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_types: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    filter_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    throttle_policy: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)

    project = relationship("Project", lazy="joined")
    endpoint = relationship("NotificationEndpoint", lazy="joined")
