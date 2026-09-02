"""ORM：Run Reporter/Analyzer 扩展结果。"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin


class RunExtensionResult(Base, TimestampMixin):
    __tablename__ = "run_extension_results"
    __table_args__ = (
        UniqueConstraint(
            "run_pk",
            "extension_point",
            "plugin_id",
            "plugin_version",
            name="uq_run_extension_results_plugin",
        ),
        Index("ix_run_extension_results_run_point", "run_pk", "extension_point"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    extension_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    run_pk: Mapped[int] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False)
    extension_point: Mapped[str] = mapped_column(String(16), nullable=False)
    plugin_id: Mapped[str] = mapped_column(String(255), nullable=False)
    plugin_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    derived_artifact_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    run = relationship("TaskRun", lazy="joined")
