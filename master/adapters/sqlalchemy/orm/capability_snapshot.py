"""M2 节点能力和 Agent 诊断快照 ORM。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class NodeCapabilitySnapshot(Base, TimestampMixin):
    __tablename__ = "node_capability_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "node_id",
            "session_id",
            "revision",
            name="uq_node_capability_snapshots_node_session_revision",
        ),
        Index(
            "ix_node_capability_snapshots_node_created",
            "node_id",
            "created_at",
        ),
        CheckConstraint("revision >= 1", name="ck_node_capability_snapshots_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    reported_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class AgentDiagnosticsSnapshot(Base, TimestampMixin):
    __tablename__ = "agent_diagnostics_snapshots"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_agent_diagnostics_snapshots_request_id"),
        UniqueConstraint(
            "node_id",
            "collected_at",
            name="uq_agent_diagnostics_snapshots_node_collected",
        ),
        Index(
            "ix_agent_diagnostics_snapshots_node_collected",
            "node_id",
            "collected_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
