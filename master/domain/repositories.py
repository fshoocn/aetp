"""领域仓储接口（Port）。

仓储接口只依赖领域对象，不依赖任何 ORM / 数据库实现；
具体实现位于 adapters/sqlalchemy/repositories/。

服务层通过 UnitOfWork 访问仓储，保证一次业务操作内
多个仓储共享同一事务。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from aetp_protocol.ids import BusinessId, PluginId, RequestId, SemVer, Sha256
from aetp_protocol.plugin_types import PluginPoint, PluginStatus

from master.domain.enums import DisconnectReason, ShardStatus
from master.domain.models import (
    AgentDiagnosticsSnapshotRecord,
    AgentLogEventRecord,
    AgentPluginDesiredVersionRecord,
    AgentPluginSyncOperationRecord,
    AuditLog,
    Device,
    DomainEvent,
    ExecutionPlanRecord,
    InboxMessage,
    Node,
    NodeCapabilitySnapshotRecord,
    NodeSession,
    OutboxMessage,
    PluginVersionRecord,
    Project,
    ProjectMember,
    ProjectMemberWithUser,
    ProjectNodeBinding,
    ProjectNodeBindingView,
    RefreshToken,
    ResourceLeaseRecord,
    RunArtifact,
    RunCaseResult,
    RunLog,
    RunResult,
    RunShard,
    ScriptCase,
    SecretValueRecord,
    ShardAttempt,
    TaskRun,
    TestScript,
    TestTask,
    User,
)
from master.domain.models.ci_integration import (
    CiTriggerBinding,
    CiWebhookDelivery,
    ProjectIntegration,
)
from master.domain.models.hook_execution import HookExecution
from master.domain.models.maintenance import NodeMaintenanceLockRecord, RemoteOperationRecord
from master.domain.models.notification import (
    EventDelivery,
    EventSubscription,
    NotificationEndpoint,
)
from master.domain.models.task_schedule import TaskSchedule
from master.domain.models.v2_task import ScriptDefinitionRecord, V2TestTaskRecord


class UserRepository(ABC):
    @abstractmethod
    def get_by_id(self, user_id: int) -> User | None: ...

    @abstractmethod
    def get_by_username(self, username: str) -> User | None: ...

    @abstractmethod
    def list(self, *, account_status: str | None = None, limit: int = 50, offset: int = 0) -> list[User]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def add(self, user: User) -> User: ...

    @abstractmethod
    def update(self, user: User) -> User: ...


class RefreshTokenRepository(ABC):
    @abstractmethod
    def get_by_hash(self, token_hash: str) -> RefreshToken | None: ...

    @abstractmethod
    def add(self, token: RefreshToken) -> RefreshToken: ...

    @abstractmethod
    def update(self, token: RefreshToken) -> RefreshToken: ...

    @abstractmethod
    def revoke_all_for_user(self, user_id: int) -> int: ...


class TestScriptRepository(ABC):
    @abstractmethod
    def get_by_script_id(self, script_id: str) -> TestScript | None: ...

    @abstractmethod
    def get_by_hash(self, sha256: str, *, project_id: str) -> TestScript | None: ...

    @abstractmethod
    def find_by_name_version(self, project_id: str, name: str, version: int) -> TestScript | None: ...

    @abstractmethod
    def max_version_for_name(self, project_id: str, name: str) -> int: ...

    @abstractmethod
    def list_by_project(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list[TestScript]: ...

    @abstractmethod
    def add(self, script: TestScript) -> TestScript: ...

    @abstractmethod
    def update(self, script: TestScript) -> TestScript: ...

    @abstractmethod
    def delete(self, script_id: str) -> None: ...

    @abstractmethod
    def list_all_file_refs(self) -> list[str]: ...


class ScriptCaseRepository(ABC):
    @abstractmethod
    def list_by_script(self, script_id: str, *, include_deleted: bool = False) -> list[ScriptCase]: ...

    @abstractmethod
    def get_by_stable_key(self, script_id: str, stable_key: str) -> ScriptCase | None: ...

    @abstractmethod
    def add(self, case: ScriptCase) -> ScriptCase: ...

    @abstractmethod
    def add_many(self, cases: list[ScriptCase]) -> list[ScriptCase]: ...

    @abstractmethod
    def update(self, case: ScriptCase) -> ScriptCase: ...

    @abstractmethod
    def delete_by_script(self, script_id: str) -> None: ...


class TestTaskRepository(ABC):
    """测试任务定义仓储（P3.3，定义与执行分离）。"""

    @abstractmethod
    def get_by_task_id(self, task_id: str, project_id: str | None = None) -> TestTask | None: ...

    @abstractmethod
    def find_by_name(self, project_id: str, name: str) -> TestTask | None: ...

    @abstractmethod
    def list_by_project(
        self,
        project_id: str,
        *,
        enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TestTask]: ...

    @abstractmethod
    def count_by_script(self, script_id: str) -> int: ...

    @abstractmethod
    def count_runs_by_task(self, task_pk: int) -> int: ...

    @abstractmethod
    def delete(self, task_pk: int) -> None: ...

    @abstractmethod
    def cleanup_disabled_for_script(self, script_id: str) -> dict[str, int]: ...

    @abstractmethod
    def add(self, task: TestTask) -> TestTask: ...

    @abstractmethod
    def update(self, task: TestTask) -> TestTask: ...


class ScriptDefinitionRepository(ABC):
    """V2 ScriptDefinition revision 仓储。"""

    @abstractmethod
    def get(self, script_definition_id: BusinessId, revision: int) -> ScriptDefinitionRecord | None: ...

    @abstractmethod
    def list_by_project(
        self,
        project_id: BusinessId,
        *,
        enabled: bool | None = None,
    ) -> list[ScriptDefinitionRecord]: ...

    @abstractmethod
    def add(self, record: ScriptDefinitionRecord) -> ScriptDefinitionRecord: ...


class V2TestTaskRepository(ABC):
    """V2 多脚本 TestTask revision 仓储。"""

    @abstractmethod
    def get(self, task_id: BusinessId, revision: int | None = None) -> V2TestTaskRecord | None: ...

    @abstractmethod
    def add(self, record: V2TestTaskRecord) -> V2TestTaskRecord: ...


class TaskRunRepository(ABC):
    """Run 执行仓储（P3.4，task_runs 表）。"""

    @abstractmethod
    def add(self, run: TaskRun) -> TaskRun: ...

    @abstractmethod
    def get_by_run_id(self, run_id: str, project_id: str | None = None) -> TaskRun | None: ...

    @abstractmethod
    def list(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        trigger_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskRun]: ...

    @abstractmethod
    def update(self, run: TaskRun) -> TaskRun: ...

    @abstractmethod
    def list_non_terminal(self, limit: int = 1000) -> list[TaskRun]: ...

    @abstractmethod
    def nullify_task_for_runs(self, task_id: str) -> int: ...


class RunShardRepository(ABC):
    """Shard 仓储（P3.4，run_shards 表）。"""

    @abstractmethod
    def add(self, shard: RunShard) -> RunShard: ...

    @abstractmethod
    def add_many(self, shards: list[RunShard]) -> list[RunShard]: ...

    @abstractmethod
    def get_by_shard_id(self, shard_id: str) -> RunShard | None: ...

    @abstractmethod
    def list_by_run(self, run_id: str) -> list[RunShard]: ...

    @abstractmethod
    def update(self, shard: RunShard) -> RunShard: ...

    @abstractmethod
    def list_by_status(self, *statuses: ShardStatus) -> list[RunShard]: ...


class ShardAttemptRepository(ABC):
    """Shard 派发尝试仓储（P3.4，shard_attempts 表，D-20 历史全量保留）。"""

    @abstractmethod
    def add(self, attempt: ShardAttempt) -> ShardAttempt: ...

    @abstractmethod
    def get_by_shard_attempt(self, shard_id: str, attempt_no: int) -> ShardAttempt | None: ...

    @abstractmethod
    def get_by_attempt_id(self, attempt_id: str) -> ShardAttempt | None: ...

    @abstractmethod
    def list_by_shard(self, shard_id: str) -> list[ShardAttempt]: ...

    @abstractmethod
    def list_by_run(self, run_id: str) -> list[ShardAttempt]: ...

    @abstractmethod
    def update(self, attempt: ShardAttempt) -> ShardAttempt: ...

    @abstractmethod
    def list_active_by_node(self, node_id: str) -> list[ShardAttempt]: ...


class RunCaseResultRepository(ABC):
    """case 级结果仓储（P3.4，run_case_results 表，D-20 按 attempt 全量保留）。"""

    @abstractmethod
    def add_many(self, results: list[RunCaseResult]) -> list[RunCaseResult]: ...

    @abstractmethod
    def list_by_run(self, run_id: str) -> list[RunCaseResult]: ...

    @abstractmethod
    def list_by_shard(self, run_id: str, shard_id: str) -> list[RunCaseResult]: ...

    @abstractmethod
    def get_by_key(self, run_id: str, shard_id: str, case_key: str, attempt_no: int) -> RunCaseResult | None: ...

    @abstractmethod
    def update(self, result: RunCaseResult) -> RunCaseResult: ...


class RunArtifactRepository(ABC):
    """结束产物仓储（P3.4，run_artifacts 表）。"""

    @abstractmethod
    def add(self, artifact: RunArtifact) -> RunArtifact: ...

    @abstractmethod
    def get_by_artifact_id(self, artifact_id: str) -> RunArtifact | None: ...

    @abstractmethod
    def get_by_file_ref(self, file_ref: str) -> RunArtifact | None: ...

    @abstractmethod
    def list_by_run(self, run_id: str) -> list[RunArtifact]: ...

    @abstractmethod
    def list_all_file_refs(self) -> list[str]: ...


class RunLogRepository(ABC):
    """Run 执行日志仓储（P6.4，run_logs 表，(run_id, sequence) 幂等）。"""

    @abstractmethod
    def add(self, log: RunLog) -> RunLog: ...

    @abstractmethod
    def add_many(self, logs: list[RunLog]) -> list[RunLog]: ...

    @abstractmethod
    def exists(self, run_id: str, sequence: int) -> bool: ...

    @abstractmethod
    def existing_sequences(self, run_id: str, sequences: list[int]) -> set[int]: ...

    @abstractmethod
    def existing_attempt_sequences(
        self,
        run_id: str,
        attempt_id: str,
        sequences: list[int],
    ) -> set[int]: ...

    @abstractmethod
    def list_by_run(self, run_id: str, *, after_sequence: int = 0) -> list[RunLog]: ...

    @abstractmethod
    def get_max_sequence(self, run_id: str) -> int: ...


class AgentLogRepository(ABC):
    """Agent 结构化日志索引仓储。"""

    @abstractmethod
    def existing_sequences(
        self,
        node_id: BusinessId,
        session_id: str,
        sequences: list[int],
    ) -> set[int]: ...

    @abstractmethod
    def add_many(self, records: list[AgentLogEventRecord]) -> list[AgentLogEventRecord]: ...

    @abstractmethod
    def list(
        self,
        node_id: BusinessId,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
        level: str | None = None,
        component: str | None = None,
        event_code: str | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        plugin_id: str | None = None,
        keyword: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
    ) -> list[AgentLogEventRecord]: ...


class RemoteOperationRepository(ABC):
    """Agent 远程运维操作仓储。"""

    @abstractmethod
    def get(self, operation_id: BusinessId) -> RemoteOperationRecord | None: ...

    @abstractmethod
    def add(self, operation: RemoteOperationRecord) -> RemoteOperationRecord: ...

    @abstractmethod
    def update(self, operation: RemoteOperationRecord) -> RemoteOperationRecord: ...

    @abstractmethod
    def list_by_node(self, node_id: BusinessId, *, limit: int = 100) -> list[RemoteOperationRecord]: ...


class NodeMaintenanceLockRepository(ABC):
    """节点维护锁仓储；同一节点最多一个活动锁。"""

    @abstractmethod
    def get(self, node_id: BusinessId) -> NodeMaintenanceLockRecord | None: ...

    @abstractmethod
    def acquire(self, lock: NodeMaintenanceLockRecord) -> NodeMaintenanceLockRecord: ...

    @abstractmethod
    def release(self, node_id: BusinessId, operation_id: BusinessId | None = None) -> bool: ...

    @abstractmethod
    def is_locked(self, node_id: BusinessId) -> bool: ...


class RunResultRepository(ABC):
    """Run 级汇总投影仓储（P3.4，results 表，run_pk 唯一）。"""

    @abstractmethod
    def add(self, result: RunResult) -> RunResult: ...

    @abstractmethod
    def get_by_run_id(self, run_id: str) -> RunResult | None: ...

    @abstractmethod
    def update(self, result: RunResult) -> RunResult: ...

    @abstractmethod
    def nullify_task_for_results(self, task_id: str) -> int: ...


class InboxMessageRepository(ABC):
    """入站消息去重仓储（P3.5，inbox_messages 表，(origin_id, message_id) 唯一）。"""

    @abstractmethod
    def get_by_origin_message(self, origin_id: str, message_id: str) -> InboxMessage | None: ...

    @abstractmethod
    def add(self, message: InboxMessage) -> InboxMessage: ...

    @abstractmethod
    def mark_processed(self, message: InboxMessage) -> InboxMessage: ...

    @abstractmethod
    def list_unprocessed(self, *, limit: int = 100) -> list[InboxMessage]: ...


class OutboxMessageRepository(ABC):
    """事务性 outbox 仓储（P3.5，outbox_messages 表）。"""

    @abstractmethod
    def enqueue(self, message: OutboxMessage) -> OutboxMessage: ...

    @abstractmethod
    def get_by_outbox_id(self, outbox_id: str) -> OutboxMessage | None: ...

    @abstractmethod
    def claim_due(self, *, limit: int = 100, now: datetime | None = None) -> list[OutboxMessage]: ...

    @abstractmethod
    def update(self, message: OutboxMessage) -> OutboxMessage: ...


class DomainEventRepository(ABC):
    """不可变领域事件仓储（P3.5，domain_events 表，sequence 唯一保证顺序）。"""

    @abstractmethod
    def add(self, event: DomainEvent) -> DomainEvent: ...

    @abstractmethod
    def get_by_event_id(self, event_id: str) -> DomainEvent | None: ...

    @abstractmethod
    def list(
        self,
        *,
        project_id: str | None = None,
        after_sequence: int | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]: ...

    @abstractmethod
    def list_by_aggregate(
        self,
        aggregate_id: str,
        *,
        project_id: str | None = None,
        limit: int = 500,
    ) -> list[DomainEvent]: ...


class AuditLogRepository(ABC):
    """审计日志仓储（P3.5，audit_logs 表，append-only）。"""

    @abstractmethod
    def add(self, log: AuditLog) -> AuditLog: ...

    @abstractmethod
    def get_by_audit_id(self, audit_id: str) -> AuditLog | None: ...

    @abstractmethod
    def list(
        self,
        *,
        project_id: str | None = None,
        actor_id: int | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]: ...


class ProjectRepository(ABC):
    @abstractmethod
    def get_by_project_id(self, project_id: str) -> Project | None: ...

    @abstractmethod
    def get_by_key(self, project_key: str) -> Project | None: ...

    @abstractmethod
    def list_all(self, *, limit: int = 100, offset: int = 0) -> list[Project]: ...

    @abstractmethod
    def list_visible_to_user(self, user_id: int, *, limit: int = 100, offset: int = 0) -> list[Project]: ...

    @abstractmethod
    def add(self, project: Project) -> Project: ...

    @abstractmethod
    def update(self, project: Project) -> Project: ...


class ProjectMemberRepository(ABC):
    @abstractmethod
    def get_role(self, project_id: str, user_id: int) -> str | None: ...

    @abstractmethod
    def list_with_users(self, project_id: str) -> list[ProjectMemberWithUser]: ...

    @abstractmethod
    def get_by_project_and_user(self, project_id: str, user_id: int) -> ProjectMember | None: ...

    @abstractmethod
    def count_owners(self, project_id: str) -> int: ...

    @abstractmethod
    def add(self, member: ProjectMember) -> ProjectMember: ...

    @abstractmethod
    def update(self, member: ProjectMember) -> ProjectMember: ...

    @abstractmethod
    def remove(self, member: ProjectMember) -> None: ...


class NodeRepository(ABC):
    @abstractmethod
    def list_all(self, *, online: bool | None = None, enabled: bool | None = None) -> list[Node]: ...

    @abstractmethod
    def get_by_id(self, node_id: str) -> Node | None: ...

    @abstractmethod
    def save(self, node: Node) -> Node:
        """创建或更新节点（按 node_id upsert；返回持久化后的节点）。"""

    @abstractmethod
    def mark_all_offline(self) -> int:
        """把所有节点投影重置为 offline（启动恢复用）。"""


class ExecutionPlanRepository(ABC):
    """V2 ExecutionPlan 不可变快照仓储。"""

    @abstractmethod
    def get_by_plan_id(self, plan_id: BusinessId) -> ExecutionPlanRecord | None: ...

    @abstractmethod
    def get_by_attempt(
        self,
        run_id: BusinessId,
        script_binding_id: BusinessId,
        shard_id: BusinessId,
        attempt_no: int,
    ) -> ExecutionPlanRecord | None: ...

    @abstractmethod
    def list_by_run(self, run_id: BusinessId) -> list[ExecutionPlanRecord]: ...

    @abstractmethod
    def add(self, record: ExecutionPlanRecord) -> ExecutionPlanRecord: ...


class ResourceLeaseRepository(ABC):
    """V2 ResourceLease 条件更新仓储。"""

    @abstractmethod
    def get_by_lease_id(self, lease_id: BusinessId) -> ResourceLeaseRecord | None: ...

    @abstractmethod
    def get_active_by_resource(self, resource_id: BusinessId) -> ResourceLeaseRecord | None: ...

    @abstractmethod
    def list_by_attempt(self, attempt_id: BusinessId) -> list[ResourceLeaseRecord]: ...

    @abstractmethod
    def add(self, record: ResourceLeaseRecord) -> ResourceLeaseRecord: ...

    @abstractmethod
    def renew(
        self,
        lease_id: BusinessId,
        *,
        expected_revision: int,
        requested_expires_at: datetime,
        now: datetime,
    ) -> ResourceLeaseRecord | None: ...

    @abstractmethod
    def release(
        self,
        lease_id: BusinessId,
        *,
        now: datetime,
        expected_revision: int | None = None,
    ) -> ResourceLeaseRecord | None: ...

    @abstractmethod
    def expire_due(self, *, now: datetime) -> list[ResourceLeaseRecord]: ...


class NodeCapabilitySnapshotRepository(ABC):
    """节点能力快照仓储：按 session/revision 保存不可变替换快照。"""

    @abstractmethod
    def get_latest(self, node_id: BusinessId) -> NodeCapabilitySnapshotRecord | None: ...

    @abstractmethod
    def list_by_node(self, node_id: BusinessId, *, limit: int = 100) -> list[NodeCapabilitySnapshotRecord]: ...

    @abstractmethod
    def add_if_newer(self, record: NodeCapabilitySnapshotRecord) -> bool: ...


class AgentDiagnosticsSnapshotRepository(ABC):
    """Agent 诊断快照仓储：按 request_id 幂等、按时间不可变保存。"""

    @abstractmethod
    def get_latest(self, node_id: BusinessId) -> AgentDiagnosticsSnapshotRecord | None: ...

    @abstractmethod
    def get_by_request_id(self, request_id: RequestId) -> AgentDiagnosticsSnapshotRecord | None: ...

    @abstractmethod
    def add(self, record: AgentDiagnosticsSnapshotRecord) -> AgentDiagnosticsSnapshotRecord: ...


class NodeSessionRepository(ABC):
    """节点会话仓储（P4.4，node_sessions 表）。

    会话用于隔离旧连接：每次进程启动一个新 session_id，新会话注册时
    旧会话关闭（SESSION_REPLACED），旧 session 的后续消息被拒绝。
    """

    @abstractmethod
    def get(self, node_pk: int, session_id: str) -> NodeSession | None: ...

    @abstractmethod
    def get_current(self, node_pk: int) -> NodeSession | None:
        """返回节点当前未关闭的会话（同一时刻至多一个有效会话）。"""

    @abstractmethod
    def create(self, session: NodeSession) -> NodeSession: ...

    @abstractmethod
    def close(self, session: NodeSession, *, reason: DisconnectReason, at: datetime | None = None) -> NodeSession:
        """关闭会话（置 disconnected_at + disconnect_reason）。"""

    @abstractmethod
    def close_all_open(self, *, reason: DisconnectReason, at: datetime | None = None) -> int:
        """关闭所有未关闭会话（启动恢复用）。"""


class DeviceRepository(ABC):
    @abstractmethod
    def add(self, device: Device) -> Device: ...

    @abstractmethod
    def update(self, device: Device) -> Device: ...

    @abstractmethod
    def list_all(self, *, online: bool | None = None) -> list[Device]: ...

    @abstractmethod
    def get_by_id(self, device_id: str) -> Device | None: ...

    @abstractmethod
    def list_for_project(self, project_id: str, *, online: bool | None = None) -> list[Device]: ...

    @abstractmethod
    def get_for_project(self, project_id: str, device_id: str) -> Device | None: ...


class ProjectNodeBindingRepository(ABC):
    @abstractmethod
    def list_with_nodes(self, project_id: str) -> list[ProjectNodeBindingView]: ...

    @abstractmethod
    def get(self, project_id: str, node_id: str) -> ProjectNodeBinding | None: ...

    @abstractmethod
    def add(self, binding: ProjectNodeBinding) -> ProjectNodeBinding: ...

    @abstractmethod
    def update(self, binding: ProjectNodeBinding) -> ProjectNodeBinding: ...

    @abstractmethod
    def remove(self, binding: ProjectNodeBinding) -> None: ...


class PluginVersionRepository(ABC):
    @abstractmethod
    def get(self, plugin_id: PluginId, version: SemVer) -> PluginVersionRecord | None: ...

    @abstractmethod
    def get_by_archive_sha256(self, archive_sha256: Sha256) -> PluginVersionRecord | None: ...

    @abstractmethod
    def list(
        self,
        *,
        point: PluginPoint | None = None,
        status: PluginStatus | None = None,
    ) -> list[PluginVersionRecord]: ...

    @abstractmethod
    def add(self, record: PluginVersionRecord) -> PluginVersionRecord: ...

    @abstractmethod
    def update(self, record: PluginVersionRecord) -> PluginVersionRecord: ...


class AgentPluginDesiredVersionRepository(ABC):
    @abstractmethod
    def get(self, node_id: BusinessId, plugin_id: PluginId) -> AgentPluginDesiredVersionRecord | None: ...

    @abstractmethod
    def list_by_node(self, node_id: BusinessId) -> list[AgentPluginDesiredVersionRecord]: ...

    @abstractmethod
    def upsert(self, record: AgentPluginDesiredVersionRecord) -> AgentPluginDesiredVersionRecord: ...

    @abstractmethod
    def remove(self, node_id: BusinessId, plugin_id: PluginId) -> None: ...


class AgentPluginSyncOperationRepository(ABC):
    @abstractmethod
    def get(self, sync_id: BusinessId) -> AgentPluginSyncOperationRecord | None: ...

    @abstractmethod
    def list_by_node(self, node_id: BusinessId) -> list[AgentPluginSyncOperationRecord]: ...

    @abstractmethod
    def add(self, record: AgentPluginSyncOperationRecord) -> AgentPluginSyncOperationRecord: ...

    @abstractmethod
    def update(self, record: AgentPluginSyncOperationRecord) -> AgentPluginSyncOperationRecord: ...


class UnitOfWork(ABC):
    """工作单元：一个业务事务内共享同一数据库会话。

    仓储通过属性访问（如 uow.users / uow.tasks）。
    使用方式：with uow() as uow: ... （正常提交，异常回滚）
    """

    users: UserRepository
    refresh_tokens: RefreshTokenRepository
    test_scripts: TestScriptRepository
    script_cases: ScriptCaseRepository
    secret_values: SecretValueRepository
    test_tasks: TestTaskRepository
    script_definitions: ScriptDefinitionRepository
    v2_test_tasks: V2TestTaskRepository
    task_runs: TaskRunRepository
    run_shards: RunShardRepository
    shard_attempts: ShardAttemptRepository
    run_case_results: RunCaseResultRepository
    run_artifacts: RunArtifactRepository
    run_logs: RunLogRepository
    agent_logs: AgentLogRepository
    remote_operations: RemoteOperationRepository
    maintenance_locks: NodeMaintenanceLockRepository
    run_results: RunResultRepository
    inbox_messages: InboxMessageRepository
    outbox_messages: OutboxMessageRepository
    domain_events: DomainEventRepository
    audit_logs: AuditLogRepository
    projects: ProjectRepository
    members: ProjectMemberRepository
    nodes: NodeRepository
    execution_plans: ExecutionPlanRepository
    resource_leases: ResourceLeaseRepository
    node_capability_snapshots: NodeCapabilitySnapshotRepository
    agent_diagnostics_snapshots: AgentDiagnosticsSnapshotRepository
    node_sessions: NodeSessionRepository
    devices: DeviceRepository
    bindings: ProjectNodeBindingRepository
    notification_endpoints: NotificationEndpointRepository
    event_subscriptions: EventSubscriptionRepository
    event_deliveries: EventDeliveryRepository
    task_schedules: TaskScheduleRepository
    project_integrations: ProjectIntegrationRepository
    ci_trigger_bindings: CiTriggerBindingRepository
    ci_webhook_deliveries: CiWebhookDeliveryRepository
    hook_executions: HookExecutionRepository
    plugin_versions: PluginVersionRepository
    agent_plugin_desired_versions: AgentPluginDesiredVersionRepository
    agent_plugin_sync_operations: AgentPluginSyncOperationRepository

    @abstractmethod
    def __enter__(self) -> UnitOfWork: ...

    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...


class SecretValueRepository(ABC):
    """加密密钥仓储（§12.2：密文落库，业务层只持有 secret_ref）。"""

    @abstractmethod
    def get(self, secret_ref: str) -> SecretValueRecord | None: ...

    @abstractmethod
    def upsert(self, secret_ref: str, cipher_text: str) -> SecretValueRecord: ...

    @abstractmethod
    def delete(self, secret_ref: str) -> None: ...


class NotificationEndpointRepository(ABC):
    """通知端点仓储（P7.6，§10.5）。"""

    @abstractmethod
    def get_by_endpoint_id(self, endpoint_id: str) -> NotificationEndpoint | None: ...

    @abstractmethod
    def list_by_project(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list[NotificationEndpoint]: ...

    @abstractmethod
    def add(self, endpoint: NotificationEndpoint) -> NotificationEndpoint: ...

    @abstractmethod
    def update(self, endpoint: NotificationEndpoint) -> NotificationEndpoint: ...

    @abstractmethod
    def delete(self, endpoint_id: str) -> None: ...


class EventSubscriptionRepository(ABC):
    """事件订阅仓储（P7.6，§10.5）。"""

    @abstractmethod
    def get_by_subscription_id(self, subscription_id: str) -> EventSubscription | None: ...

    @abstractmethod
    def list_by_project(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list[EventSubscription]: ...

    @abstractmethod
    def add(self, subscription: EventSubscription) -> EventSubscription: ...

    @abstractmethod
    def update(self, subscription: EventSubscription) -> EventSubscription: ...

    @abstractmethod
    def delete(self, subscription_id: str) -> None: ...


class EventDeliveryRepository(ABC):
    """投递记录仓储（P7.6，§10.5）。"""

    @abstractmethod
    def get_by_delivery_id(self, delivery_id: str) -> EventDelivery | None: ...

    @abstractmethod
    def list_by_project(
        self,
        project_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EventDelivery]: ...

    @abstractmethod
    def get_by_event_subscription(self, event_id: str, subscription_id: str) -> EventDelivery | None: ...

    @abstractmethod
    def add(self, delivery: EventDelivery) -> EventDelivery: ...

    @abstractmethod
    def update(self, delivery: EventDelivery) -> EventDelivery: ...


class TaskScheduleRepository(ABC):
    """任务调度计划仓储（P8.2，D-18）。"""

    @abstractmethod
    def get_by_schedule_id(self, schedule_id: str) -> TaskSchedule | None: ...

    @abstractmethod
    def list_by_task(self, task_id: str) -> list[TaskSchedule]: ...

    @abstractmethod
    def list_due(self, *, now: datetime, limit: int = 100) -> list[TaskSchedule]: ...

    @abstractmethod
    def add(self, schedule: TaskSchedule) -> TaskSchedule: ...

    @abstractmethod
    def update(self, schedule: TaskSchedule) -> TaskSchedule: ...

    @abstractmethod
    def delete(self, schedule_id: str) -> None: ...


class ProjectIntegrationRepository(ABC):
    """项目 CI/CD 集成仓储（P8.3）。"""

    @abstractmethod
    def get_by_integration_id(self, integration_id: str) -> ProjectIntegration | None: ...

    @abstractmethod
    def list_by_project(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list[ProjectIntegration]: ...

    @abstractmethod
    def add(self, integration: ProjectIntegration) -> ProjectIntegration: ...

    @abstractmethod
    def update(self, integration: ProjectIntegration) -> ProjectIntegration: ...

    @abstractmethod
    def delete(self, integration_id: str) -> None: ...


class CiTriggerBindingRepository(ABC):
    """CI 触发绑定仓储（P8.3）。"""

    @abstractmethod
    def get_by_binding_id(self, binding_id: str) -> CiTriggerBinding | None: ...

    @abstractmethod
    def list_by_integration(
        self, integration_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[CiTriggerBinding]: ...

    @abstractmethod
    def add(self, binding: CiTriggerBinding) -> CiTriggerBinding: ...

    @abstractmethod
    def update(self, binding: CiTriggerBinding) -> CiTriggerBinding: ...

    @abstractmethod
    def delete(self, binding_id: str) -> None: ...


class CiWebhookDeliveryRepository(ABC):
    """CI Webhook 投递记录仓储（P8.3）。"""

    @abstractmethod
    def get_by_integration_delivery(self, integration_id: str, delivery_id: str) -> CiWebhookDelivery | None: ...

    @abstractmethod
    def list_by_integration(
        self, integration_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[CiWebhookDelivery]: ...

    @abstractmethod
    def add(self, delivery: CiWebhookDelivery) -> CiWebhookDelivery: ...

    @abstractmethod
    def update(self, delivery: CiWebhookDelivery) -> CiWebhookDelivery: ...


class HookExecutionRepository(ABC):
    """Hook 执行审计仓储（P8.4，§10.6）。"""

    @abstractmethod
    def list_by_project(
        self,
        project_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HookExecution]: ...

    @abstractmethod
    def add(self, execution: HookExecution) -> HookExecution: ...
