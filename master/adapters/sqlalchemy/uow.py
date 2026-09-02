"""SQLAlchemy 工作单元实现。

一个业务事务内共享同一 Session，所有仓储绑定到该 Session，
保证跨仓储操作原子性（正常提交 / 异常回滚）。

仓储属性类型以基类 UnitOfWork 的接口注解为准（依赖倒置），
不在本类中重声明；具体实现统一在 __enter__ 中绑定 Session 后创建。
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.database_interface import DatabaseInterface
from master.adapters.sqlalchemy.repositories import (
    AgentDiagnosticsSnapshotRepositoryImpl,
    AgentLogRepositoryImpl,
    AgentPluginDesiredVersionRepositoryImpl,
    AgentPluginSyncOperationRepositoryImpl,
    AuditLogRepositoryImpl,
    CiTriggerBindingRepositoryImpl,
    CiWebhookDeliveryRepositoryImpl,
    DeviceRepositoryImpl,
    DomainEventRepositoryImpl,
    EventDeliveryRepositoryImpl,
    EventSubscriptionRepositoryImpl,
    ExecutionPlanRepositoryImpl,
    HookExecutionRepositoryImpl,
    IdempotencyRecordRepositoryImpl,
    InboxMessageRepositoryImpl,
    NodeCapabilitySnapshotRepositoryImpl,
    NodeMaintenanceLockRepositoryImpl,
    NodeRepositoryImpl,
    NodeSessionRepositoryImpl,
    NotificationEndpointRepositoryImpl,
    OutboxMessageRepositoryImpl,
    PluginVersionRepositoryImpl,
    ProjectIntegrationRepositoryImpl,
    ProjectMemberRepositoryImpl,
    ProjectNodeBindingRepositoryImpl,
    ProjectRepositoryImpl,
    RefreshTokenRepositoryImpl,
    RemoteOperationRepositoryImpl,
    ResourceLeaseRepositoryImpl,
    RunArtifactRepositoryImpl,
    RunCaseResultRepositoryImpl,
    RunExtensionResultRepositoryImpl,
    RunLogRepositoryImpl,
    RunResultRepositoryImpl,
    RunShardRepositoryImpl,
    ScriptCaseRepositoryImpl,
    ScriptDefinitionRepositoryImpl,
    SecretValueRepositoryImpl,
    ShardAttemptRepositoryImpl,
    TaskRunRepositoryImpl,
    TaskScheduleRepositoryImpl,
    TestScriptRepositoryImpl,
    TestTaskRepositoryImpl,
    UserRepositoryImpl,
    V2TestTaskRepositoryImpl,
)
from master.domain.repositories import UnitOfWork


class SqlAlchemyUnitOfWork(UnitOfWork):
    def __init__(self, database: DatabaseInterface, *, include_legacy_relations: bool = True) -> None:
        self._database = database
        self._include_legacy_relations = include_legacy_relations
        self._session: Session | None = None

    def __enter__(self) -> Self:
        session = self._database.session()
        self._session = session
        self.users = UserRepositoryImpl(session)
        self.refresh_tokens = RefreshTokenRepositoryImpl(session)
        self.test_scripts = TestScriptRepositoryImpl(session)
        self.script_cases = ScriptCaseRepositoryImpl(session)
        self.secret_values = SecretValueRepositoryImpl(session)
        self.test_tasks = TestTaskRepositoryImpl(session)
        self.script_definitions = ScriptDefinitionRepositoryImpl(session)
        self.v2_test_tasks = V2TestTaskRepositoryImpl(session)
        self.task_runs = TaskRunRepositoryImpl(
            session,
            include_legacy_task=self._include_legacy_relations,
        )
        self.run_shards = RunShardRepositoryImpl(session)
        self.shard_attempts = ShardAttemptRepositoryImpl(session)
        self.run_case_results = RunCaseResultRepositoryImpl(session)
        self.run_artifacts = RunArtifactRepositoryImpl(session)
        self.run_logs = RunLogRepositoryImpl(session)
        self.agent_logs = AgentLogRepositoryImpl(session)
        self.run_results = RunResultRepositoryImpl(session)
        self.run_extension_results = RunExtensionResultRepositoryImpl(session)
        self.inbox_messages = InboxMessageRepositoryImpl(session)
        self.remote_operations = RemoteOperationRepositoryImpl(session)
        self.maintenance_locks = NodeMaintenanceLockRepositoryImpl(session)
        self.outbox_messages = OutboxMessageRepositoryImpl(session)
        self.domain_events = DomainEventRepositoryImpl(session)
        self.audit_logs = AuditLogRepositoryImpl(session)
        self.projects = ProjectRepositoryImpl(session)
        self.members = ProjectMemberRepositoryImpl(session)
        self.nodes = NodeRepositoryImpl(session)
        self.execution_plans = ExecutionPlanRepositoryImpl(session)
        self.resource_leases = ResourceLeaseRepositoryImpl(session)
        self.node_capability_snapshots = NodeCapabilitySnapshotRepositoryImpl(session)
        self.agent_diagnostics_snapshots = AgentDiagnosticsSnapshotRepositoryImpl(session)
        self.node_sessions = NodeSessionRepositoryImpl(session)
        self.devices = DeviceRepositoryImpl(session)
        self.bindings = ProjectNodeBindingRepositoryImpl(session)
        self.notification_endpoints = NotificationEndpointRepositoryImpl(session)
        self.event_subscriptions = EventSubscriptionRepositoryImpl(session)
        self.event_deliveries = EventDeliveryRepositoryImpl(session)
        self.task_schedules = TaskScheduleRepositoryImpl(session)
        self.project_integrations = ProjectIntegrationRepositoryImpl(session)
        self.ci_trigger_bindings = CiTriggerBindingRepositoryImpl(session)
        self.ci_webhook_deliveries = CiWebhookDeliveryRepositoryImpl(session)
        self.hook_executions = HookExecutionRepositoryImpl(session)
        self.idempotency_records = IdempotencyRecordRepositoryImpl(session)
        self.plugin_versions = PluginVersionRepositoryImpl(session)
        self.agent_plugin_desired_versions = AgentPluginDesiredVersionRepositoryImpl(session)
        self.agent_plugin_sync_operations = AgentPluginSyncOperationRepositoryImpl(session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None


class SqlAlchemyUnitOfWorkFactory:
    """SqlAlchemyUnitOfWork 的工厂：每次调用返回一个新 UoW（一个新事务）。

    自身无状态（仅持有 database），可在 DI 容器中作为进程级单例注入；
    服务通过 `with self._uow_factory() as uow:` 获得相互独立的事务。
    """

    def __init__(self, database: DatabaseInterface) -> None:
        self._database = database

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(
            self._database,
            include_legacy_relations=not self._database.config.v2_only,
        )
