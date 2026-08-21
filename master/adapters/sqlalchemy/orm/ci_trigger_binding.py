"""ORM：CI 触发绑定（§8.8）。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin


class CiTriggerBinding(Base, TimestampMixin):
    __tablename__ = "ci_trigger_bindings"
    __table_args__ = (
        UniqueConstraint(
            "integration_pk",
            "task_pk",
            name="uq_ci_trigger_bindings_integration_task",
        ),
        Index("ix_ci_trigger_bindings_integration_enabled", "integration_pk", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    integration_pk: Mapped[int] = mapped_column(
        ForeignKey("project_integrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_pk: Mapped[int] = mapped_column(
        ForeignKey("test_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_filter_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    parameter_mapping_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    integration = relationship("ProjectIntegration", lazy="joined")
    task = relationship("TestTask", lazy="joined")
