"""SQLAlchemy ORM 模型：统一从此处导入。

导入本包会注册所有表的 metadata（供 Alembic 迁移使用）。
"""

from __future__ import annotations

from .agent_log import AgentLogEvent
from .audit_log import AuditLog
from .base import (
    NAMING_CONVENTION,
    Base,
    JSONType,
    TimestampMixin,
    UTCDateTime,
    utcnow,
)
from .capability_snapshot import AgentDiagnosticsSnapshot, NodeCapabilitySnapshot
from .ci_trigger_binding import CiTriggerBinding
from .ci_webhook_delivery import CiWebhookDelivery
from .device import Device
from .domain_event import DomainEvent
from .event_delivery import EventDelivery
from .event_subscription import EventSubscription
from .execution_plan import ExecutionPlan
from .hook_execution import HookExecution
from .inbox_message import InboxMessage
from .maintenance import NodeMaintenanceLock, RemoteOperation
from .node import Node
from .node_session import NodeSession
from .notification_endpoint import NotificationEndpoint
from .outbox_message import OutboxMessage
from .plugin_governance import AgentPluginDesiredVersion, AgentPluginSyncOperation, PluginVersion
from .project import Project
from .project_integration import ProjectIntegration
from .project_member import ProjectMember
from .project_node_binding import ProjectNodeBinding
from .refresh_token import RefreshToken
from .resource_lease import ResourceLease
from .run_artifact import RunArtifact
from .run_case_result import RunCaseResult
from .run_log import RunLog
from .run_result import RunResult
from .run_shard import RunShard
from .script_case import ScriptCase
from .secret_value import SecretValue
from .shard_attempt import ShardAttempt
from .task_run import TaskRun
from .task_schedule import TaskSchedule
from .test_script import TestScript
from .test_task import TestTask
from .user import User
from .v2_task import ScriptDefinition, TestTaskScript, V2TestTask

__all__ = [
    "NAMING_CONVENTION",
    "AuditLog",
    "AgentLogEvent",
    "Base",
    "CiTriggerBinding",
    "CiWebhookDelivery",
    "AgentDiagnosticsSnapshot",
    "Device",
    "DomainEvent",
    "ExecutionPlan",
    "EventDelivery",
    "EventSubscription",
    "HookExecution",
    "InboxMessage",
    "RemoteOperation",
    "NodeMaintenanceLock",
    "JSONType",
    "Node",
    "NodeCapabilitySnapshot",
    "NodeSession",
    "NotificationEndpoint",
    "OutboxMessage",
    "Project",
    "ProjectIntegration",
    "ProjectMember",
    "ProjectNodeBinding",
    "PluginVersion",
    "AgentPluginDesiredVersion",
    "AgentPluginSyncOperation",
    "RefreshToken",
    "ResourceLease",
    "RunArtifact",
    "RunCaseResult",
    "RunLog",
    "RunResult",
    "RunShard",
    "ScriptCase",
    "SecretValue",
    "ShardAttempt",
    "TaskRun",
    "TaskSchedule",
    "TestScript",
    "TestTask",
    "ScriptDefinition",
    "TestTaskScript",
    "V2TestTask",
    "TimestampMixin",
    "UTCDateTime",
    "User",
    "utcnow",
]
