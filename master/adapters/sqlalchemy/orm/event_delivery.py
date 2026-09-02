"""ORM：通知投递记录。

每个 (event_id, subscription_id) 唯一，投递结果全量保留。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class EventDelivery(Base, TimestampMixin):
    __tablename__ = "event_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "subscription_id",
            "dedupe_key",
            name="uq_event_deliveries_event_subscription_dedupe",
        ),
        Index("ix_event_deliveries_project_status", "project_pk", "status"),
        Index("ix_event_deliveries_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    project_pk: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subscription_id: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    aggregation_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    window_ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    response_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    project = relationship("Project", lazy="joined")
