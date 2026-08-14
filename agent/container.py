"""Agent 依赖注入容器（P5.1 骨架）。

Agent 各阶段依赖（本地账本、Transport、插件 registry、执行器等）按
P5.2~P5.7 逐步装配；本阶段只提供进程级配置单例，作为组合根骨架。

组合根（入口）先调用 ``agent.config.configure()`` 再创建容器，
容器内通过 ``get_settings()`` 只读获取配置。
"""

from __future__ import annotations

from dependency_injector import containers, providers

from agent.config import get_settings


class Container(containers.DeclarativeContainer):
    """Agent 应用容器。"""

    # 进程级配置单例（组合根 configure() 之后才求值）
    settings = providers.Singleton(lambda: get_settings())
