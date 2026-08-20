"""ORM：Hook 执行审计记录（§10.6）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UTCDateTime


class HookExecution(Base, TimestampMixin):
    __tablename__ = "hook_executions"
    __table_args__ = (
        Index("ix_hook_executions_event_hook", "event_id", "hook_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hook_name: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
