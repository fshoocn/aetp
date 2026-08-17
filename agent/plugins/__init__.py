"""Agent 侧插件模块（P5.5）。"""

from agent.plugins.errors import (
    PLUGIN_INSTALL_FAILED,
    PLUGIN_LOAD_FAILED,
    PLUGIN_NOT_FOUND,
    PLUGIN_VERSION_MISMATCH,
    PluginError,
    PluginInstallError,
    PluginLoadError,
    PluginNotFoundError,
    PluginVersionMismatchError,
)
from agent.plugins.execution import (
    AgentExecutionPlugin,
    AgentPluginCapability,
    AgentTaskContext,
    AgentPluginRegistry,
)

__all__ = [
    "AgentExecutionPlugin",
    "AgentPluginRegistry",
    "AgentPluginCapability",
    "AgentTaskContext",
    "PluginError",
    "PluginInstallError",
    "PluginLoadError",
    "PluginNotFoundError",
    "PluginVersionMismatchError",
    "PLUGIN_INSTALL_FAILED",
    "PLUGIN_LOAD_FAILED",
    "PLUGIN_NOT_FOUND",
    "PLUGIN_VERSION_MISMATCH",
]
