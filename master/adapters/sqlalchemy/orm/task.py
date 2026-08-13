"""ORM：测试任务。

project_pk / device_pk / created_by 使用代理主键 int 外键，
保证数据库级参照完整性；业务标识 task_id 对外暴露。
command / result 为结构化 JSON 列。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from master.domain.enums import TaskStatus

from .base import Base, JSONType, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .device import Device
    from .project import Project


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_project_status", "project_pk", "status"),
        Index("ix_tasks_created_at", "created_at"),
        CheckConstraint(
            "status IN ('pending','dispatching','running','cancelling',"
            "'succeeded','failed','cancelled','timed_out')",
            name="ck_tasks_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    project_pk: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    device_pk: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskStatus.PENDING.value
    )
    command: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    result: Mapped[Optional[dict]] = mapped_column(JSONType, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )

    project: Mapped["Project"] = relationship()
    device: Mapped["Device"] = relationship()
