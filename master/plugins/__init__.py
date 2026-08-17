"""Master 侧任务类型插件包（P3.8/P5.5）。"""

from __future__ import annotations

from .base import (
    CaseInfo,
    MasterTaskPlugin,
    ShardSpec,
    TaskDefinitionSpec,
)
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
from .registry import PluginRegistry, create_default_registry

__all__ = [
    "MasterTaskPlugin",
    "CaseInfo",
    "ShardSpec",
    "TaskDefinitionSpec",
    "PluginRegistry",
    "create_default_registry",
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
