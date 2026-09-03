"""依赖注入容器（dependency-injector）。

统一管理应用级依赖：
- database:       数据库实例（单例，创建时执行迁移）
- uow_factory:    工作单元工厂（每个业务操作一个事务）
- event_bus:      SSE 事件总线（单例）
- auth_service:   认证服务（依赖 uow_factory）
- task_service:   任务服务
- device_service: 设备服务
- ...

组合根（run.py / main.py lifespan）创建容器并初始化；
各层通过依赖注入获取实例，不自行 new。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from dependency_injector import containers, providers

from common.secret_derivation import derive_hex
from master.adapters.mqtt.transport import MqttTransport
from master.adapters.notifications.senders import build_default_registry
from master.adapters.secrets.encrypted_store import EncryptedSecretStore
from master.adapters.sqlalchemy.database_factory import create_database
from master.adapters.sqlalchemy.database_interface import DatabaseInterface
from master.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from master.adapters.sse.event_bus import EventBus
from master.adapters.storage.local_storage import LocalStorage
from master.application.services.agent_log_service import AgentLogService
from master.application.services.agent_maintenance_service import AgentMaintenanceService
from master.application.services.artifact_service import ArtifactService
from master.application.services.artifact_storage_service import ArtifactStorageService
from master.application.services.artifact_upload_signing_service import ArtifactUploadSigningService
from master.application.services.auth_service import AuthService
from master.application.services.capability_snapshot_service import (
    CapabilitySnapshotProjectionService,
    DiagnosticsSnapshotProjectionService,
    NodeCapabilityRevisionCache,
)
from master.application.services.diagnostics_request_service import DiagnosticsRequestService
from master.application.services.event_publisher import EventPublisher
from master.application.services.execution_service import ExecutionService
from master.application.services.hook_runner import HookRunner
from master.application.services.idempotency_service import IdempotencyService
from master.application.services.message_router import MasterMessageRouter
from master.application.services.mqtt_runtime import MasterMqttRuntime
from master.application.services.node_matching_service import NodeMatchingService
from master.application.services.node_presence_service import NodePresenceService
from master.application.services.node_service import NodeService
from master.application.services.notification_dispatcher import NotificationDispatcher
from master.application.services.notification_service import NotificationService
from master.application.services.plan_lease_service import PlanLeaseService
from master.application.services.plan_materialization_service import PlanMaterializationService
from master.application.services.plugin_download_service import PluginDownloadService
from master.application.services.plugin_governance_service import PluginGovernanceService
from master.application.services.plugin_sync_service import PluginSyncService
from master.application.services.project_member_service import ProjectMemberService
from master.application.services.project_node_binding_service import (
    ProjectNodeBindingService,
)
from master.application.services.project_service import ProjectService
from master.application.services.recovery_service import RecoveryService
from master.application.services.reporting_pipeline import (
    ReportPipeline,
    build_default_reporting_registries,
)
from master.application.services.schedule_service import ScheduleService
from master.application.services.scheduler_service import SchedulerService
from master.application.services.script_definition_service import ScriptDefinitionService
from master.application.services.script_download_service import ScriptDownloadService
from master.application.services.script_storage_service import ScriptStorageService
from master.application.services.storage_cleanup_service import StorageCleanupService
from master.application.services.task_service import TaskService
from master.config import get_settings, runtime_dir
from master.domain.node_matcher import NodeMatcher
from master.plugins.extension_resolver import ExtensionResolver
from master.plugins.registry import PluginRegistry
from master.workers.event_hook_worker import EventHookWorker
from master.workers.maintenance_worker import MaintenanceWorker
from master.workers.outbox_worker import OutboxWorker


def _init_database(url: str) -> DatabaseInterface:
    """创建数据库实例并完成建表 + 迁移。"""
    db = create_database(url)
    db.connect()
    return db


def _internal_signing_secret() -> str:
    """内部签名下载密钥：由主密钥按 ``internal-signing`` 用途派生。

    独立配置 ``AETP_MASTER_INTERNAL_SIGNING_SECRET`` 优先；缺省时用 HKDF
    从 JWT 主密钥派生（与 JWT 签名、SecretStore 加密密钥隔离）。
    """
    settings = get_settings()
    if settings.internal_signing_secret:
        return settings.internal_signing_secret
    return derive_hex(settings.jwt_secret, "internal-signing")


def _data_dir() -> Path:
    """外部数据目录。"""
    settings = get_settings()
    if settings.data_dir is not None:
        return settings.data_dir
    return runtime_dir() / "data"


def _artifact_upload_url(
    run_id: str,
    project_id: str,
    node_id: str,
    shard_id: str,
    attempt_id: str | None = None,
) -> str:
    """构造绑定 Run/Node/Shard/Attempt 的限时 Artifact 上传地址。"""
    return ArtifactUploadSigningService(
        _internal_signing_secret(),
        base_url=get_settings().public_base_url,
        ttl_s=get_settings().internal_download_ttl_s,
    ).build_url(run_id, project_id, node_id, shard_id, attempt_id)


class Container(containers.DeclarativeContainer):
    """AETP Master 应用容器。"""

    # 惰性读取进程级配置（组合根 configure() 之后才求值）
    database_url = providers.Callable(lambda: get_settings().database_url)

    # 数据库：进程级单例
    database = providers.Singleton(
        _init_database,
        database_url,
    )

    # 工作单元工厂：进程级单例（工厂本身无状态，仅持有 database；
    # 每次调用返回一个新 UoW = 一个新事务）
    uow_factory = providers.Singleton(SqlAlchemyUnitOfWorkFactory, database=database)

    # SSE 事件总线：进程级单例
    event_bus = providers.Singleton(EventBus)

    # 加密密钥存储（SecretStore 端口实现，§12.2）：Fernet 加密落库，
    # 通知端点与 CI 集成的密钥经它持久化，重启后仍可解回。
    secret_store = providers.Singleton(
        EncryptedSecretStore,
        uow_factory=uow_factory,
        master_secret=providers.Callable(lambda: get_settings().jwt_secret),
    )

    # P7.6/P8.5：通知端点/订阅管理服务（密钥经 SecretStore 持久化，不回显）
    notification_service = providers.Singleton(
        NotificationService,
        uow_factory=uow_factory,
        secret_store=secret_store,
    )

    # P8.5：通知 Sender Adapters 注册中心 + 分发器（必须在 event_publisher 之前）
    sender_registry = providers.Singleton(build_default_registry)
    notification_dispatcher = providers.Factory(
        NotificationDispatcher,
        uow_factory=uow_factory,
        registry=sender_registry,
        # 密钥经 NotificationService.get_secret 解回（Singleton 保证同一实例），
        # 否则所有需要密钥的 sender（如 generic_webhook HMAC 签名）永远拿不到密钥。
        get_secret=notification_service.provided.get_secret,
    )

    # P8.4：生命周期 Hook（准入 fail-closed、事件 fail-open、审计）
    hook_runner = providers.Factory(
        HookRunner,
        uow_factory=uow_factory,
    )

    # 事件 Hook 后台消费 worker（异步消费，不阻塞 SSE/通知）
    event_hook_worker = providers.Singleton(
        EventHookWorker,
        hook_runner=hook_runner,
    )

    agent_log_service = providers.Singleton(
        AgentLogService,
        uow_factory=uow_factory,
        master_id=providers.Callable(lambda: get_settings().mqtt_client_id),
    )
    agent_maintenance_service = providers.Singleton(
        AgentMaintenanceService,
        uow_factory=uow_factory,
        master_id=providers.Callable(lambda: get_settings().mqtt_client_id),
    )

    # Master 任务类型插件注册表：解析、验证、分片、硬件需求和 Agent 包元数据
    plugin_download_service = providers.Factory(
        PluginDownloadService,
        secret=providers.Callable(_internal_signing_secret),
        base_url=providers.Callable(lambda: get_settings().public_base_url),
        ttl_s=providers.Callable(lambda: get_settings().internal_download_ttl_s),
    )
    artifact_upload_signing_service = providers.Factory(
        ArtifactUploadSigningService,
        secret=providers.Callable(_internal_signing_secret),
        base_url=providers.Callable(lambda: get_settings().public_base_url),
        ttl_s=providers.Callable(lambda: get_settings().internal_download_ttl_s),
    )
    plugin_governance_service = providers.Singleton(
        PluginGovernanceService,
        uow_factory=uow_factory,
        archive_root=providers.Callable(lambda: _data_dir() / "plugins"),
    )
    plugin_registry = providers.Singleton(
        PluginRegistry,
        archive_root=providers.Callable(lambda: _data_dir() / "plugins"),
    )
    master_extension_resolver = providers.Singleton(
        ExtensionResolver,
        registry=plugin_registry,
        extraction_root=providers.Callable(lambda: _data_dir() / "plugins" / "runtime"),
    )
    plugin_sync_service = providers.Singleton(
        PluginSyncService,
        uow_factory=uow_factory,
        package_url_builder=plugin_download_service.provided.build_versioned_download_url,
        master_id=providers.Callable(lambda: get_settings().mqtt_client_id),
    )
    plan_lease_service = providers.Singleton(
        PlanLeaseService,
        uow_factory=uow_factory,
        master_id=providers.Callable(lambda: get_settings().mqtt_client_id),
    )
    plan_materialization_service = providers.Factory(
        PlanMaterializationService,
        uow_factory=uow_factory,
        plan_leases=plan_lease_service,
    )
    execution_service = providers.Singleton(
        ExecutionService,
        uow_factory=uow_factory,
        plan_leases=plan_lease_service,
        master_id=providers.Callable(lambda: get_settings().mqtt_client_id),
    )
    # 文件存储：进程级单例（默认本地 data/ 目录；切云存储只换 adapter）
    storage = providers.Singleton(
        LocalStorage,
        root=providers.Callable(_data_dir),
    )

    # 脚本文件存储服务（P4.7：上传/下载统一走 Storage 端口）
    script_storage_service = providers.Factory(ScriptStorageService, storage=storage)

    # 产物文件存储服务（P6.6：run_artifacts 文件读写统一走 Storage 端口）
    artifact_storage_service = providers.Factory(ArtifactStorageService, storage=storage)

    # 存储孤儿文件清理服务（§6.2 补充：对比 DB file_ref 集合删除无引用对象）
    storage_cleanup_service = providers.Factory(
        StorageCleanupService,
        uow_factory=uow_factory,
        storage=storage,
    )

    # 产物登记/查询服务（P6.6：写引用 + 项目范围查询）
    artifact_service = providers.Factory(
        ArtifactService,
        uow_factory=uow_factory,
        storage=artifact_storage_service,
    )

    # M6：Run 事实提交后由独立 Reporter/Analyzer 插件处理。
    reporter_registry = providers.Singleton(
        lambda resolver: build_default_reporting_registries(resolver)[0],
        master_extension_resolver,
    )
    analyzer_registry = providers.Singleton(
        lambda resolver: build_default_reporting_registries(resolver)[1],
        master_extension_resolver,
    )
    report_pipeline = providers.Factory(
        ReportPipeline,
        uow_factory=uow_factory,
        storage=artifact_storage_service,
        reporters=reporter_registry,
        analyzers=analyzer_registry,
    )

    # P7.1：领域事件先持久化，再广播到项目范围 SSE；P8.5：分发通知。
    event_publisher = providers.Singleton(
        EventPublisher,
        uow_factory=uow_factory,
        event_bus=event_bus,
        notification_dispatcher=notification_dispatcher,
        event_hook_worker=event_hook_worker,
        report_pipeline=report_pipeline,
    )

    # 认证服务
    auth_service = providers.Factory(AuthService, uow_factory=uow_factory)
    idempotency_service = providers.Singleton(
        IdempotencyService,
        uow_factory=uow_factory,
        ttl_s=providers.Callable(lambda: get_settings().idempotency_ttl_s),
    )

    # 项目服务
    project_service = providers.Factory(ProjectService, uow_factory=uow_factory)

    # 项目成员授权服务
    project_member_service = providers.Factory(ProjectMemberService, uow_factory=uow_factory)

    # 项目节点绑定服务
    project_node_binding_service = providers.Factory(ProjectNodeBindingService, uow_factory=uow_factory)

    # Node/Device 平台资产只读查询服务
    node_service = providers.Factory(NodeService, uow_factory=uow_factory)

    # 崩溃恢复服务（§8.6：节点离线恢复 + 启动扫描 + 超时检测）
    recovery_service = providers.Factory(
        RecoveryService,
        uow_factory=uow_factory,
        stale_timeout=providers.Callable(lambda: timedelta(seconds=get_settings().run_stale_timeout_s)),
    )

    # 节点在线投影服务（P4.4：注册/心跳/LWT/会话校验）
    node_presence_service = providers.Factory(
        NodePresenceService,
        uow_factory=uow_factory,
        recovery_service=recovery_service,
    )

    capability_snapshot_cache = providers.Singleton(NodeCapabilityRevisionCache)
    capability_snapshot_service = providers.Singleton(
        CapabilitySnapshotProjectionService,
        uow_factory=uow_factory,
        cache=capability_snapshot_cache,
    )
    diagnostics_snapshot_service = providers.Factory(
        DiagnosticsSnapshotProjectionService,
        uow_factory=uow_factory,
    )
    diagnostics_request_service = providers.Factory(
        DiagnosticsRequestService,
        uow_factory=uow_factory,
        master_id=providers.Callable(lambda: get_settings().mqtt_client_id),
    )
    node_matching_service = providers.Factory(
        NodeMatchingService,
        uow_factory=uow_factory,
        capability_snapshots=capability_snapshot_service,
        matcher=providers.Factory(NodeMatcher),
    )

    script_download_service = providers.Factory(
        ScriptDownloadService,
        secret=providers.Callable(_internal_signing_secret),
        base_url=providers.Callable(lambda: get_settings().public_base_url),
        ttl_s=providers.Callable(lambda: get_settings().internal_download_ttl_s),
    )
    script_definition_service = providers.Factory(
        ScriptDefinitionService,
        uow_factory=uow_factory,
        storage=script_storage_service,
        plugin_registry=plugin_registry,
        executor_resolver=master_extension_resolver,
    )
    scheduler_service = providers.Factory(
        SchedulerService,
        uow_factory=uow_factory,
        node_matching=node_matching_service,
        materializer=plan_materialization_service,
        script_url_builder=script_download_service.provided.build_download_url,
        plugin_url_builder=plugin_download_service.provided.build_versioned_download_url,
        artifact_url_builder=_artifact_upload_url,
    )

    task_service = providers.Factory(
        TaskService,
        uow_factory=uow_factory,
    )

    # 任务调度计划服务（P8.2：cron/interval 互斥，D-18）
    schedule_service = providers.Factory(
        ScheduleService,
        uow_factory=uow_factory,
        task_service=task_service,
        scheduler=scheduler_service,
        event_publisher=event_publisher,
    )

    # 入站 Agent 事件路由（P6.4：严格 Envelope 校验后投影/在线处理）
    message_router = providers.Factory(
        MasterMessageRouter,
        node_presence=node_presence_service,
        event_publisher=event_publisher,
        scheduler=scheduler_service,
        uow_factory=uow_factory,
        capability_snapshot=capability_snapshot_service,
        diagnostics_snapshot=diagnostics_snapshot_service,
        plugin_sync=plugin_sync_service,
        execution=execution_service,
        agent_logs=agent_log_service,
        maintenance=agent_maintenance_service,
    )

    # Master MQTT 传输（P4.2；未配置 mqtt_host 时延后由 runtime 决定是否启动）
    mqtt_transport = providers.Singleton(MqttTransport, settings=providers.Callable(get_settings))

    # Outbox worker（P4.3：事务性 outbox 可靠发送 run.assign/register-ack）
    outbox_worker = providers.Factory(
        OutboxWorker,
        uow_factory=uow_factory,
        transport=mqtt_transport,
        max_attempts=providers.Callable(lambda: get_settings().outbox_max_attempts),
    )

    # Master MQTT 运行时（P6.4：订阅事件 → 路由投影 + outbox 发送）
    mqtt_runtime = providers.Factory(
        MasterMqttRuntime,
        transport=mqtt_transport,
        router=message_router,
        outbox_worker=outbox_worker,
    )

    # 后台维护 worker（P8.2/P8.5：Schedule tick + Stale Run 检测 + 孤儿清理）
    maintenance_worker = providers.Singleton(
        MaintenanceWorker,
        schedule_service=schedule_service,
        recovery_service=recovery_service,
        plan_lease_service=plan_lease_service,
        storage_cleanup_service=storage_cleanup_service,
        notification_dispatcher=notification_dispatcher,
        interval_s=providers.Callable(lambda: get_settings().maintenance_interval_s),
    )
