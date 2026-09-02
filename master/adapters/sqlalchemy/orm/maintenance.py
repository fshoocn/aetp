"""ORM：Agent 远程运维操作和节点维护锁。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class RemoteOperation(Base, TimestampMixin):
    __tablename__ = "agent_maintenance_operations"
    __table_args__ = (
        Index("ix_agent_maintenance_operations_node_created", "node_id", "created_at"),
        CheckConstraint(
            "kind IN ('diagnostics','plugin_sync','log_level','drain','restart')",
            name="ck_agent_maintenance_operations_kind",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')",
            name="ck_agent_maintenance_operations_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expected_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request: Mapped[dict] = mapped_column(JSONType, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(String(2048), nullable=False, default="")


class NodeMaintenanceLock(Base):
    __tablename__ = "node_maintenance_locks"
    __table_args__ = (
        Index("ix_node_maintenance_locks_operation", "operation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
