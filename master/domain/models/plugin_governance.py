"""V2 插件版本治理领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, PluginId, SemVer, SessionId, Sha256
from aetp_protocol.plugin_types import (
    DesiredPluginVersion,
    PluginPoint,
    PluginStatus,
)
from aetp_protocol.plugins import PluginManifest, PluginSyncItem, PluginSyncItemResult


class PluginSyncOperationState(StrEnum):
    PENDING = "pending"
    DRAINING = "draining"
    INSTALLING = "installing"
    RESTARTING = "restarting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PluginVersionRecord:
    id: int | None
    plugin_id: PluginId
    version: SemVer
    point: PluginPoint
    status: PluginStatus
    filename: str
    archive_sha256: Sha256
    manifest_sha256: Sha256
    manifest: PluginManifest
    archive_path: str
    installed_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class AgentPluginDesiredVersionRecord:
    id: int | None
    node_id: BusinessId
    desired: DesiredPluginVersion
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class AgentPluginSyncOperationRecord:
    id: int | None
    sync_id: BusinessId
    node_id: BusinessId
    expected_session_id: SessionId
    state: PluginSyncOperationState
    items: tuple[PluginSyncItem, ...]
    results: tuple[PluginSyncItemResult, ...] | None
    accepted: bool | None
    restart_required: bool
    error_code: ErrorCode | None
    completed_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
