""" 插件枚举和不可变引用，供各协议模块共享。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, RootModel

from .ids import PluginId, RelativePath, SemVer, Sha256


class PluginPoint(StrEnum):
    EXECUTOR = "executor"
    RESOURCE = "resource"
    RUNTIME = "runtime"
    SOFTWARE = "software"
    REPORTER = "reporter"
    ANALYZER = "analyzer"
    NOTIFIER = "notifier"
    INTEGRATION = "integration"
    HOOK = "hook"
    SHARDING = "sharding"
    UI = "ui"
    STORAGE = "storage"
    TRANSPORT = "transport"
    IDENTITY = "identity"


class PluginStatus(StrEnum):
    UPLOADED = "uploaded"
    VERIFIED = "verified"
    INSTALLED = "installed"
    PENDING_RESTART = "pending_restart"
    ENABLED = "enabled"
    DISABLED = "disabled"
    REMOVED = "removed"
    ERROR = "error"


class PluginAvailability(StrEnum):
    AVAILABLE = "available"
    BLOCKED = "blocked"
    UPDATING = "updating"
    ERROR = "error"
    NOT_INSTALLED = "not_installed"


class PluginSyncAction(StrEnum):
    INSTALL = "install"
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    REMOVE = "remove"


class PluginRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: PluginId
    version: SemVer
    archive_sha256: Sha256


class PluginDistributionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: PluginId
    version: SemVer
    archive_sha256: Sha256
    download_url: str | None = None


class DesiredPluginVersion(BaseModel):
    """Master 针对节点的精确插件版本期望。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: PluginId
    point: PluginPoint
    version: SemVer
    auto_update: bool = True
    maintenance_window: str | None = None


class EntrypointRef(RootModel[str]):
    root: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


class RelativeUiEntry(RootModel[RelativePath]):
    root: RelativePath
