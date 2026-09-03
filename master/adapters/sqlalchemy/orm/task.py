"""ORM：ScriptDefinition、TestTask revision 和脚本绑定。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin

if TYPE_CHECKING:
    from .user import User


class ScriptDefinition(Base, TimestampMixin):
    __tablename__ = "script_definitions"
    __table_args__ = (
        UniqueConstraint(
            "script_definition_id",
            "revision",
            name="uq_script_definitions_definition_revision",
        ),
        Index("ix_script_definitions_project_enabled", "project_id", "enabled"),
        CheckConstraint("revision >= 1", name="ck_script_definitions_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    script_definition_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    executor_plugin_id: Mapped[str] = mapped_column(String(255), nullable=False)
    executor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    executor_archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    cases: Mapped[list[dict[str, object]]] = mapped_column(JSONType, nullable=False)
    requirement: Mapped[dict[str, object] | None] = mapped_column(JSONType, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class TestTask(Base, TimestampMixin):
    __tablename__ = "test_tasks"
    __table_args__ = (
        UniqueConstraint("task_id", "revision", name="uq_test_tasks_task_revision"),
        Index("ix_test_tasks_project_enabled", "project_id", "enabled"),
        CheckConstraint("revision >= 1", name="ck_test_tasks_revision"),
        CheckConstraint(
            "execution_mode IN ('parallel','sequence')",
            name="ck_test_tasks_execution_mode",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="parallel")
    stop_on_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_policy: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    node_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)

    scripts: Mapped[list[TestTaskScript]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TestTaskScript.order_index",
        lazy="selectin",
    )
    creator: Mapped[User] = relationship()


class TestTaskScript(Base, TimestampMixin):
    __tablename__ = "test_task_scripts"
    __table_args__ = (
        UniqueConstraint(
            "task_pk",
            "task_revision",
            "binding_id",
            name="uq_test_task_scripts_binding",
        ),
        UniqueConstraint(
            "task_pk",
            "task_revision",
            "order_index",
            name="uq_test_task_scripts_order",
        ),
        Index("ix_test_task_scripts_definition", "script_definition_id", "script_revision"),
        CheckConstraint("script_revision >= 1", name="ck_test_task_scripts_revision"),
        CheckConstraint("order_index >= 0", name="ck_test_task_scripts_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_pk: Mapped[int] = mapped_column(ForeignKey("test_tasks.id", ondelete="CASCADE"), nullable=False)
    task_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    script_definition_id: Mapped[str] = mapped_column(String(64), nullable=False)
    script_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    case_selection: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    split_policy: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    task: Mapped[TestTask] = relationship(back_populates="scripts")
