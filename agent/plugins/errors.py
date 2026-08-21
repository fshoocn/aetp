"""Agent 执行插件错误与机器可读错误码。"""

from __future__ import annotations

PLUGIN_NOT_FOUND = "PLUGIN_NOT_FOUND"
PLUGIN_VERSION_MISMATCH = "PLUGIN_VERSION_MISMATCH"
PLUGIN_INSTALL_FAILED = "PLUGIN_INSTALL_FAILED"
PLUGIN_LOAD_FAILED = "PLUGIN_LOAD_FAILED"


class PluginError(ValueError):
    code = "PLUGIN_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class PluginNotFoundError(PluginError):
    code = PLUGIN_NOT_FOUND


class PluginVersionMismatchError(PluginError):
    code = PLUGIN_VERSION_MISMATCH


class PluginInstallError(PluginError):
    code = PLUGIN_INSTALL_FAILED


class PluginLoadError(PluginError):
    code = PLUGIN_LOAD_FAILED
