""" 插件 Manifest、扩展点和版本引用 DTO。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ErrorCode
from .execution import ResourceRequirement, RuntimeRequirement, SoftwareRequirement
from .ids import BusinessId, CapabilityName, PluginId, RelativePath, SemVer, SessionId, VersionRange
from .plugin_types import (
    EntrypointRef,
    PluginDistributionRef,
    PluginPoint,
    PluginSyncAction,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PluginEntrypoints(_Strict):
    master: EntrypointRef | None = None
    agent: EntrypointRef | None = None
    ui: RelativePath | None = None


class StaticRequirements(_Strict):
    runtimes: tuple[RuntimeRequirement, ...] = ()
    software: tuple[SoftwareRequirement, ...] = ()
    resources: tuple[ResourceRequirement, ...] = ()


class PluginManifest(_Strict):
    schema_version: Literal[2]
    id: PluginId
    version: SemVer
    api_version: SemVer
    point: PluginPoint
    display_name: str = Field(min_length=1, max_length=255)
    entrypoints: PluginEntrypoints
    capabilities: tuple[CapabilityName, ...] = ()
    static_requirements: StaticRequirements = Field(default_factory=StaticRequirements)
    configuration_schema: RelativePath | None = None
    ui_protocol_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_point_entrypoints(self) -> PluginManifest:
        required = {
            PluginPoint.EXECUTOR: ("master", "agent"),
            PluginPoint.RESOURCE: ("agent",),
            PluginPoint.RUNTIME: ("agent",),
            PluginPoint.SOFTWARE: ("agent",),
            PluginPoint.REPORTER: ("master",),
            PluginPoint.ANALYZER: ("master",),
            PluginPoint.NOTIFIER: ("master",),
            PluginPoint.INTEGRATION: ("master",),
            PluginPoint.HOOK: ("master",),
            PluginPoint.SHARDING: ("master",),
            PluginPoint.UI: ("ui",),
            PluginPoint.STORAGE: ("master", "agent"),
            PluginPoint.TRANSPORT: ("master", "agent"),
            PluginPoint.IDENTITY: ("master",),
        }.get(self.point, ())
        missing = tuple(name for name in required if getattr(self.entrypoints, name) is None)
        if missing:
            raise ValueError(f"point {self.point.value} requires entrypoints: {', '.join(missing)}")
        return self


class PluginRequirement(_Strict):
    plugin_id: PluginId
    version: VersionRange


class PluginSyncItem(_Strict):
    plugin_id: PluginId
    point: PluginPoint = PluginPoint.EXECUTOR
    version: SemVer
    action: PluginSyncAction
    package: PluginDistributionRef | None = None
    expected_previous_version: SemVer | None = None

    @model_validator(mode="after")
    def validate_package(self) -> PluginSyncItem:
        if self.action is not PluginSyncAction.REMOVE and self.package is None:
            raise ValueError("install/upgrade/downgrade requires package")
        if self.action is PluginSyncAction.REMOVE and self.package is not None:
            raise ValueError("remove must not contain package")
        if self.package is not None and (
            self.package.plugin_id != self.plugin_id or self.package.version != self.version
        ):
            raise ValueError("sync package must match plugin_id and version")
        return self


class PluginSyncRequest(_Strict):
    sync_id: BusinessId
    node_id: BusinessId
    expected_session_id: SessionId
    items: tuple[PluginSyncItem, ...]
    drain_timeout_s: int = Field(default=1800, ge=0)
    restart_after: bool = True


class PluginSyncItemResult(_Strict):
    plugin_id: PluginId
    version: SemVer
    state: Literal["installed", "active", "blocked", "failed", "skipped", "removed"]
    unavailable_reasons: tuple[ErrorCode, ...] = ()
    message: str = ""


class PluginSyncResult(_Strict):
    sync_id: BusinessId
    node_id: BusinessId
    accepted: bool
    restart_required: bool
    items: tuple[PluginSyncItemResult, ...]


PluginSyncRequest.model_rebuild()
PluginSyncResult.model_rebuild()
