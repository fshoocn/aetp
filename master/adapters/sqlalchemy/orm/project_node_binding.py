"""ORM：项目-节点绑定。"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ProjectNodeBinding(Base, TimestampMixin):
    __tablename__ = "project_node_bindings"
    __table_args__ = (UniqueConstraint("project_pk", "node_pk", name="uq_project_node_bindings_project_node"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_pk: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_pk: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assigned_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
