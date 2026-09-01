"""ORM：V2 ResourceLease 和活跃资源独占约束。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UTCDateTime


class ResourceLease(Base, TimestampMixin):
    __tablename__ = "resource_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active','released','expired')",
            name="ck_resource_leases_state",
        ),
        CheckConstraint("revision >= 1", name="ck_resource_leases_revision"),
        Index("ix_resource_leases_attempt", "attempt_id", "state"),
        Index("ix_resource_leases_expires", "state", "expires_at"),
        Index(
            "uq_resource_leases_active_resource",
            "resource_id",
            unique=True,
            sqlite_where=text("state = 'active'"),
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shard_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
