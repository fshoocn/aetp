"""Agent 插件注册表工厂（P5.5）。"""

from __future__ import annotations

import logging

from agent.plugins.execution import AgentPluginRegistry

logger = logging.getLogger(__name__)


def create_default_registry(plugin_dir=None) -> AgentPluginRegistry:
    """创建默认注册表，发现共享包并恢复已安装的 Agent 执行入口。"""
    registry = AgentPluginRegistry()
    registry.discover()
    if plugin_dir is not None:
        from agent.plugins.installer import LocalPluginInstaller

        restored = LocalPluginInstaller(plugin_dir).restore(registry)
        if restored:
            logger.info("已恢复本地 Agent 插件: count=%s", restored)
    logger.info(
        "Agent 插件注册表已初始化: %s",
        registry.supported_task_types(),
    )
    return registry
