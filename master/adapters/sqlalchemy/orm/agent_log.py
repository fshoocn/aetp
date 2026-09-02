"""ORM：Agent 结构化日志索引。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class AgentLogEvent(Base, TimestampMixin):
    __tablename__ = "agent_log_events"
    __table_args__ = (
        UniqueConstraint("node_id", "session_id", "sequence", name="uq_agent_log_node_session_sequence"),
        Index("ix_agent_log_node_occurred", "node_id", "occurred_at"),
        Index("ix_agent_log_node_level_component", "node_id", "level", "component"),
        Index("ix_agent_log_run_attempt", "run_id", "attempt_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    component: Mapped[str] = mapped_column(String(128), nullable=False)
    event_code: Mapped[str] = mapped_column(String(128), nullable=False)
    message_template: Mapped[str] = mapped_column(String(1024), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plugin_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    context: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    detail: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    exception: Mapped[dict[str, object] | None] = mapped_column(JSONType, nullable=True)
    event: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    batch_first_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
