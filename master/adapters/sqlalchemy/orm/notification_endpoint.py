"""ORM：通知端点。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin


class NotificationEndpoint(Base, TimestampMixin):
    __tablename__ = "notification_endpoints"
    __table_args__ = (
        UniqueConstraint("project_pk", "name", name="uq_notification_endpoints_project_name"),
        Index("ix_notification_endpoints_project_enabled", "project_pk", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    project_pk: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    config: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    secret_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    project = relationship("Project", lazy="joined")
