"""SQLAlchemy ORM 模型：统一从此处导入。

导入本包会注册所有表的 metadata（供 Alembic 迁移使用）。
"""

from __future__ import annotations

from .audit_log import AuditLog
from .base import (
    JSONType,
    NAMING_CONVENTION,
    UTCDateTime,
    Base,
    TimestampMixin,
    utcnow,
)
from .device import Device
from .domain_event import DomainEvent
from .inbox_message import InboxMessage
from .node import Node
from .node_session import NodeSession
from .outbox_message import OutboxMessage
from .project import Project
from .project_member import ProjectMember
from .project_node_binding import ProjectNodeBinding
from .refresh_token import RefreshToken
from .run_artifact import RunArtifact
from .run_case_result import RunCaseResult
from .run_log import RunLog
from .run_result import RunResult
from .run_shard import RunShard
from .script_case import ScriptCase
from .shard_attempt import ShardAttempt
from .task import Task
from .task_log import TaskLog
from .task_run import TaskRun
from .test_script import TestScript
from .test_task import TestTask
from .user import User

__all__ = [
    "Base",
    "TimestampMixin",
    "NAMING_CONVENTION",
    "UTCDateTime",
    "JSONType",
    "utcnow",
    "User",
    "AuditLog",
    "DomainEvent",
    "InboxMessage",
    "OutboxMessage",
    "Device",
    "Node",
    "NodeSession",
    "Project",
    "ProjectMember",
    "ProjectNodeBinding",
    "RefreshToken",
    "ScriptCase",
    "TestScript",
    "TestTask",
    "TaskRun",
    "RunShard",
    "ShardAttempt",
    "RunCaseResult",
    "RunArtifact",
    "RunResult",
    "RunLog",
    "Task",
    "TaskLog",
]
