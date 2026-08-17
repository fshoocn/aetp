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
from agent.application.services.registration_service import RegistrationService
from agent.config import get_settings, resolve_sqlite_url


class Container(containers.DeclarativeContainer):
    """Agent 应用容器。"""

    # 进程级配置单例（组合根 configure() 之后才求值）
    settings = providers.Singleton(lambda: get_settings())

    # 本地账本单例（P5.2：SQLite，agent_runs/inbox/outbox/spool/cache）
    ledger = providers.Singleton(
        SQLiteLedger,
        url=providers.Callable(
            lambda: resolve_sqlite_url(get_settings().ledger_url)
        ),
        max_spool_bytes=providers.Callable(
            lambda: get_settings().task_log_spool_max_bytes
        ),
    )

    # MQTT 传输单例（P5.3：aiomqtt + 指数退避重连 + 固定 LWT）
    transport = providers.Singleton(
        AgentMqttTransport,
        settings=settings,
    )

    # 注册与心跳服务（P5.3：register outbox → register-ack 校验 → heartbeat）
    registration_service = providers.Factory(
        RegistrationService,
        transport=transport,
        ledger=ledger,
        settings=settings,
    )

    # AgentRuntime：唯一生命周期组合根（P5.3，P5.4 在 message handler 扩展）
    runtime = providers.Factory(
        AgentRuntime,
        settings=settings,
        transport=transport,
        ledger=ledger,
        registration=registration_service,
    )
