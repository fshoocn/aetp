"""SQLAlchemy 仓储实现包：统一从此处导入。"""

from __future__ import annotations

from .audit_log_repository import AuditLogRepositoryImpl
from .capability_snapshot_repository import (
    AgentDiagnosticsSnapshotRepositoryImpl,
    NodeCapabilitySnapshotRepositoryImpl,
)
from .ci_integration_repository import (
    CiTriggerBindingRepositoryImpl,
    CiWebhookDeliveryRepositoryImpl,
    ProjectIntegrationRepositoryImpl,
)
from .device_repository import DeviceRepositoryImpl
from .domain_event_repository import DomainEventRepositoryImpl
from .event_delivery_repository import EventDeliveryRepositoryImpl
from .event_subscription_repository import EventSubscriptionRepositoryImpl
from .execution_plan_repository import ExecutionPlanRepositoryImpl
from .hook_execution_repository import HookExecutionRepositoryImpl
from .inbox_message_repository import InboxMessageRepositoryImpl
from .node_repository import NodeRepositoryImpl
from .node_session_repository import NodeSessionRepositoryImpl
from .notification_endpoint_repository import NotificationEndpointRepositoryImpl
from .outbox_message_repository import OutboxMessageRepositoryImpl
from .plugin_governance_repository import (
    AgentPluginDesiredVersionRepositoryImpl,
    AgentPluginSyncOperationRepositoryImpl,
    PluginVersionRepositoryImpl,
)
from .project_member_repository import ProjectMemberRepositoryImpl
from .project_node_binding_repository import ProjectNodeBindingRepositoryImpl
from .project_repository import ProjectRepositoryImpl
from .refresh_token_repository import RefreshTokenRepositoryImpl
from .resource_lease_repository import ResourceLeaseRepositoryImpl
from .run_artifact_repository import RunArtifactRepositoryImpl
from .run_case_result_repository import RunCaseResultRepositoryImpl
from .run_log_repository import RunLogRepositoryImpl
from .run_result_repository import RunResultRepositoryImpl
from .run_shard_repository import RunShardRepositoryImpl
from .script_case_repository import ScriptCaseRepositoryImpl
from .secret_value_repository import SecretValueRepositoryImpl
from .shard_attempt_repository import ShardAttemptRepositoryImpl
from .task_run_repository import TaskRunRepositoryImpl
from .task_schedule_repository import TaskScheduleRepositoryImpl
from .test_script_repository import TestScriptRepositoryImpl
from .test_task_repository import TestTaskRepositoryImpl
from .user_repository import UserRepositoryImpl
from .v2_task_repository import ScriptDefinitionRepositoryImpl, V2TestTaskRepositoryImpl

__all__ = [
    "AuditLogRepositoryImpl",
    "CiTriggerBindingRepositoryImpl",
    "CiWebhookDeliveryRepositoryImpl",
    "AgentDiagnosticsSnapshotRepositoryImpl",
    "DeviceRepositoryImpl",
    "DomainEventRepositoryImpl",
    "ExecutionPlanRepositoryImpl",
    "EventDeliveryRepositoryImpl",
    "EventSubscriptionRepositoryImpl",
    "HookExecutionRepositoryImpl",
    "InboxMessageRepositoryImpl",
    "NodeRepositoryImpl",
    "NodeCapabilitySnapshotRepositoryImpl",
    "NodeSessionRepositoryImpl",
    "NotificationEndpointRepositoryImpl",
    "OutboxMessageRepositoryImpl",
    "ProjectIntegrationRepositoryImpl",
    "ProjectMemberRepositoryImpl",
    "ProjectNodeBindingRepositoryImpl",
    "ProjectRepositoryImpl",
    "PluginVersionRepositoryImpl",
    "AgentPluginDesiredVersionRepositoryImpl",
    "AgentPluginSyncOperationRepositoryImpl",
    "RefreshTokenRepositoryImpl",
    "ResourceLeaseRepositoryImpl",
    "RunArtifactRepositoryImpl",
    "RunCaseResultRepositoryImpl",
    "RunLogRepositoryImpl",
    "RunResultRepositoryImpl",
    "RunShardRepositoryImpl",
    "ScriptCaseRepositoryImpl",
    "SecretValueRepositoryImpl",
    "ShardAttemptRepositoryImpl",
    "TaskRunRepositoryImpl",
    "TaskScheduleRepositoryImpl",
    "TestScriptRepositoryImpl",
    "TestTaskRepositoryImpl",
    "UserRepositoryImpl",
    "ScriptDefinitionRepositoryImpl",
    "V2TestTaskRepositoryImpl",
]
