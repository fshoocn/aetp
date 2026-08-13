"""任务类型插件包（P3.8）。

Master 侧插件数据接口与注册表 + Agent 侧执行插件注册表与错误处理；
具体插件（pytest/cdd/canoe）后续阶段实现。
"""

from __future__ import annotations

from .base import CaseInfo, CaseResult, ShardSpec, TaskContext, TaskTypePlugin
from .capability import PluginCapability, filter_supported
from .errors import (
    PLUGIN_LOAD_FAILED,
    PLUGIN_NOT_FOUND,
    PLUGIN_VERSION_MISMATCH,
    PluginError,
    PluginLoadError,
    PluginNotFoundError,
    PluginVersionMismatchError,
)
from .execution import ExecutionPlugin, ExecutionPluginRegistry
from .registry import PluginRegistry

__all__ = [
    "TaskTypePlugin",
    "CaseInfo",
    "ShardSpec",
    "CaseResult",
    "TaskContext",
    "PluginRegistry",
    "ExecutionPlugin",
    "ExecutionPluginRegistry",
    "PluginCapability",
    "filter_supported",
    "PluginError",
    "PluginNotFoundError",
    "PluginVersionMismatchError",
    "PluginLoadError",
    "PLUGIN_NOT_FOUND",
    "PLUGIN_VERSION_MISMATCH",
    "PLUGIN_LOAD_FAILED",
]
