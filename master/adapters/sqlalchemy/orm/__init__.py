"""SQLAlchemy ORM 模型：统一从此处导入。

导入本包会注册所有表的 metadata（供 Alembic 迁移使用）。
"""

from __future__ import annotations

from .base import (
    JSONType,
    NAMING_CONVENTION,
    UTCDateTime,
    Base,
    TimestampMixin,
    utcnow,
)
from .device import Device
from .node import Node
from .project import Project
from .project_member import ProjectMember
from .project_node_binding import ProjectNodeBinding
from .task import Task
from .task_log import TaskLog
from .user import User

__all__ = [
    "Base",
    "TimestampMixin",
    "NAMING_CONVENTION",
    "UTCDateTime",
    "JSONType",
    "utcnow",
    "User",
    "Device",
    "Node",
    "Project",
    "ProjectMember",
    "ProjectNodeBinding",
    "Task",
    "TaskLog",
]
