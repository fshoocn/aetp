"""ORM：CI Webhook 投递记录（§8.8）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class CiWebhookDelivery(Base, TimestampMixin):
    __tablename__ = "ci_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "integration_pk", "delivery_id",
            name="uq_ci_webhook_deliveries_integration_delivery",
        ),
        Index("ix_ci_webhook_deliveries_integration_received", "integration_pk", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_pk: Mapped[int] = mapped_column(
        ForeignKey("project_integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")
    triggered_run_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    integration = relationship("ProjectIntegration", lazy="joined")
