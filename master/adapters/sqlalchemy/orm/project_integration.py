"""ORM：项目 CI/CD 集成（§8.8）。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin


class ProjectIntegration(Base, TimestampMixin):
    __tablename__ = "project_integrations"
    __table_args__ = (
        UniqueConstraint("project_pk", "name", name="uq_project_integrations_project_name"),
        Index("ix_project_integrations_project_enabled", "project_pk", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    integration_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    project_pk: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_json: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)

    project = relationship("Project", lazy="joined")
