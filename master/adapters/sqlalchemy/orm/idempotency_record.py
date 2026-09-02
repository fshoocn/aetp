"""ORM：写 API 幂等键记录。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class IdempotencyRecord(Base, TimestampMixin):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
        Index("ix_idempotency_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(512), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
