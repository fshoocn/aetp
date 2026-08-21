"""Agent 依赖注入容器（P5.1~P5.3 骨架）。

Agent 各阶段依赖（本地账本、Transport、注册服务、插件 registry、执行器
等）按 P5.2~P5.7 逐步装配。

组合根（入口）先调用 ``agent.config.configure()`` 再创建容器，
容器内通过 ``get_settings()`` 只读获取配置。
"""

from __future__ import annotations

from dependency_injector import containers, providers

from agent.adapters.mqtt.transport import AgentMqttTransport
from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.runtime import AgentRuntime
from agent.application.services.artifact_upload_service import ArtifactUploadService
from agent.application.services.capability_loader import CapabilityCache
from agent.application.services.execution_service import ExecutionService
from agent.application.services.registration_service import RegistrationService
from agent.application.services.script_cache_service import ScriptCacheService
from agent.config import get_settings, resolve_sqlite_url
from agent.plugins.installer import LocalPluginInstaller
from agent.plugins.registry import create_default_registry


class Container(containers.DeclarativeContainer):
    """Agent 应用容器。"""

    # 进程级配置单例（组合根 configure() 之后才求值）
    settings = providers.Singleton(lambda: get_settings())

    # 本地账本单例（P5.2：SQLite，agent_runs/inbox/outbox/spool/cache）
    ledger = providers.Singleton(
        SQLiteLedger,
        url=providers.Callable(lambda: resolve_sqlite_url(get_settings().ledger_url)),
        max_spool_bytes=providers.Callable(lambda: get_settings().task_log_spool_max_bytes),
    )

    # MQTT 传输单例（P5.3：aiomqtt + 指数退避重连 + 固定 LWT）
    transport = providers.Singleton(
        AgentMqttTransport,
        settings=settings,
    )

    # 插件注册表单例（P5.5：自动注册内置插件，上报 plugin_versions）
    plugin_registry = providers.Singleton(
        create_default_registry,
        plugin_dir=providers.Callable(lambda: get_settings().plugin_dir),
    )

    # 插件安装器单例（P5.5：按 Master plugin_ref 下载、校验并隔离安装）
    plugin_installer = providers.Singleton(
        LocalPluginInstaller,
        root=providers.Callable(lambda: get_settings().plugin_dir),
    )

    # 脚本下载/缓存单例（P5.6：下载 + sha256 校验 + 按 hash 本地缓存）
    script_cache = providers.Singleton(
        ScriptCacheService,
        cache_dir=providers.Callable(lambda: get_settings().script_cache_dir),
        ledger=ledger,
    )

    # Run 产物上传器（P6.6：JUnit XML/插件声明附件上传到 Master）
    artifact_uploader = providers.Singleton(ArtifactUploadService)

    # 执行服务单例（P6.1：并发上限 + timeout + cancel token + 异常映射）
    execution_service = providers.Singleton(
        ExecutionService,
        settings=settings,
        ledger=ledger,
    )

    # 能力扫描缓存单例（P5：仅可插拔外设变动时重扫，避免重复全量扫描）
    capability_cache = providers.Singleton(
        CapabilityCache,
        serial_map_file=providers.Callable(lambda: get_settings().serial_map_file),
    )

    # 注册与心跳服务（P5.3 + P5.5：register outbox → register-ack 校验 → heartbeat）
    registration_service = providers.Factory(
        RegistrationService,
        transport=transport,
        ledger=ledger,
        settings=settings,
        capabilities=providers.Callable(
            lambda cache=capability_cache: cache().scan()
        ),
        plugin_registry=plugin_registry,
    )

    # AgentRuntime：唯一生命周期组合根（P5.3 + P5.4 + P5.5 + P5.7 + P6.1）
    # CommandDispatcher / ScriptPreflightService 由 AgentRuntime 内部创建，
    # 避免 Container 中 is_registered 的循环依赖
    runtime = providers.Factory(
        AgentRuntime,
        settings=settings,
        transport=transport,
        ledger=ledger,
        registration=registration_service,
        plugin_registry=plugin_registry,
        plugin_installer=plugin_installer,
        script_cache=script_cache,
        artifact_uploader=artifact_uploader,
        execution_service=execution_service,
        capability_cache=capability_cache,
    )
