"""ORM： ExecutionPlan 不可变快照。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class ExecutionPlan(Base, TimestampMixin):
    __tablename__ = "execution_plans"
    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_execution_plans_plan_id"),
        UniqueConstraint(
            "run_id",
            "script_binding_id",
            "shard_id",
            "attempt_no",
            name="uq_execution_plans_run_binding_shard_attempt",
        ),
        Index("ix_execution_plans_node_deadline", "node_id", "deadline_at"),
        Index("ix_execution_plans_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    script_binding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    script_definition_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shard_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
