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
from agent.application.services.agent_log_facade import AgentLogFacade
from agent.application.services.artifact_upload_service import ArtifactUploadService
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.application.services.execution_service import ExecutionService
from agent.application.services.executor_resolver import ExecutorResolver
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.application.services.resource_provider_resolver import ResourceProviderResolver
from agent.application.services.script_cache_service import ScriptCacheService
from agent.config import AgentSettings, get_settings, resolve_sqlite_url
from agent.plugins.installer import PluginInstaller
from agent.plugins.registry import PluginRegistry
from common.transport import Transport


def _build_capability_publisher(
    transport: Transport,
    settings: AgentSettings,
    registry: PluginRegistry,
    resource_providers: ResourceProviderRegistry,
) -> CapabilityPublisher:
    """装配当前协议能力快照发布器。"""
    return CapabilityPublisher(
        transport,
        settings,
        registry,
        resource_providers=resource_providers,
    )


def _build_resource_provider_registry(
    settings: AgentSettings,
    resolver: ResourceProviderResolver,
) -> ResourceProviderRegistry:
    """ResourceProvider 全部来自已安装 resource 插件包（无源码内置）。

    安装 ``org.aetp.resource`` 插件包后，其入口工厂提供 serial/power/can 三个
    Provider；未安装对应插件包时注册表为空（能力快照不含这些资源，不报错）。
    """
    del settings
    return ResourceProviderRegistry((*resolver.resolve_all(),))


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

    # 插件注册表单例
    plugin_registry = providers.Singleton(
        PluginRegistry,
        root=providers.Callable(lambda: get_settings().plugin_dir),
    )

    # 插件安装器单例
    plugin_installer = providers.Singleton(
        PluginInstaller,
        root=providers.Callable(lambda: get_settings().plugin_dir),
    )

    executor_resolver = providers.Singleton(
        ExecutorResolver,
        registry=plugin_registry,
    )
    resource_provider_resolver = providers.Singleton(
        ResourceProviderResolver,
        registry=plugin_registry,
    )
    resource_provider_registry = providers.Singleton(
        _build_resource_provider_registry,
        settings=settings,
        resolver=resource_provider_resolver,
    )
    capability_publisher = providers.Factory(
        _build_capability_publisher,
        transport=transport,
        settings=settings,
        registry=plugin_registry,
        resource_providers=resource_provider_registry,
    )

    # 脚本下载/缓存单例（P5.6：下载 + sha256 校验 + 按 hash 本地缓存）
    script_cache = providers.Singleton(
        ScriptCacheService,
        cache_dir=providers.Callable(lambda: get_settings().script_cache_dir),
        ledger=ledger,
    )

    # Run 产物上传器（P6.6：JUnit XML/插件声明附件上传到 Master）
    artifact_uploader = providers.Singleton(ArtifactUploadService)
    agent_log_facade = providers.Singleton(
        AgentLogFacade,
        settings=settings,
        ledger=ledger,
    )

    # 执行服务单例（P6.1：并发上限 + timeout + cancel token + 异常映射）
    execution_service = providers.Singleton(
        ExecutionService,
        settings=settings,
        ledger=ledger,
    )

    # AgentRuntime：唯一生命周期组合根
    runtime = providers.Factory(
        AgentRuntime,
        settings=settings,
        transport=transport,
        ledger=ledger,
        plugin_registry=plugin_registry,
        plugin_installer=plugin_installer,
        script_cache=script_cache,
        artifact_uploader=artifact_uploader,
        execution_service=execution_service,
        capability_publisher=capability_publisher,
        executor_resolver=executor_resolver.provided.resolve,
        resource_providers=resource_provider_registry,
        agent_log_facade=agent_log_facade,
    )
