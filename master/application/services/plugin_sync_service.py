"""Master V2 插件期望版本和 Agent 同步操作服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from aetp_protocol.ids import BusinessId, PluginId
from aetp_protocol.plugin_types import DesiredPluginVersion, PluginStatus, PluginSyncAction
from aetp_protocol.plugins import PluginSyncItem, PluginSyncRequest, PluginSyncResult

from master.domain.models import (
    AgentPluginDesiredVersionRecord,
    AgentPluginSyncOperationRecord,
    PluginSyncOperationState,
)
from master.domain.repositories import UnitOfWork

_OPERATION_TRANSITIONS: dict[PluginSyncOperationState, frozenset[PluginSyncOperationState]] = {
    PluginSyncOperationState.PENDING: frozenset({
        PluginSyncOperationState.DRAINING,
        PluginSyncOperationState.CANCELLED,
        PluginSyncOperationState.FAILED,
        PluginSyncOperationState.SUCCEEDED,
    }),
    PluginSyncOperationState.DRAINING: frozenset({
        PluginSyncOperationState.INSTALLING,
        PluginSyncOperationState.CANCELLED,
        PluginSyncOperationState.FAILED,
    }),
    PluginSyncOperationState.INSTALLING: frozenset({
        PluginSyncOperationState.RESTARTING,
        PluginSyncOperationState.SUCCEEDED,
        PluginSyncOperationState.FAILED,
    }),
    PluginSyncOperationState.RESTARTING: frozenset({
        PluginSyncOperationState.SUCCEEDED,
        PluginSyncOperationState.FAILED,
    }),
    PluginSyncOperationState.SUCCEEDED: frozenset(),
    PluginSyncOperationState.FAILED: frozenset(),
    PluginSyncOperationState.CANCELLED: frozenset(),
}


class InvalidPluginSyncTransition(ValueError):
    """同步操作状态迁移非法。"""


class PluginSyncService:
    """维护 Master 期望版本，并持久化 Agent 同步操作事实。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def set_desired_version(
        self,
        node_id: BusinessId,
        desired: DesiredPluginVersion,
    ) -> AgentPluginDesiredVersionRecord:
        now = datetime.now(UTC)
        with self._uow_factory() as uow:
            plugin = uow.plugin_versions.get(desired.plugin_id, desired.version)
            if plugin is None or plugin.status is PluginStatus.REMOVED:
                raise ValueError("期望插件版本不存在或已移除")
            if plugin.point is not desired.point:
                raise ValueError("期望插件 point 与插件版本不一致")
            return uow.agent_plugin_desired_versions.upsert(
                AgentPluginDesiredVersionRecord(
                    id=None,
                    node_id=node_id,
                    desired=desired,
                    created_at=now,
                    updated_at=now,
                )
            )

    def remove_desired_version(self, node_id: BusinessId, plugin_id: PluginId) -> None:
        with self._uow_factory() as uow:
            uow.agent_plugin_desired_versions.remove(node_id, plugin_id)

    def create_sync_operation(
        self,
        request: PluginSyncRequest,
    ) -> AgentPluginSyncOperationRecord:
        now = datetime.now(UTC)
        with self._uow_factory() as uow:
            existing = uow.agent_plugin_sync_operations.get(request.sync_id)
            if existing is not None:
                if existing.node_id != request.node_id or existing.items != request.items:
                    raise ValueError("sync_id 已用于不同的同步请求")
                return existing
            self._validate_items(uow, request.items)
            return uow.agent_plugin_sync_operations.add(
                AgentPluginSyncOperationRecord(
                    id=None,
                    sync_id=request.sync_id,
                    node_id=request.node_id,
                    expected_session_id=request.expected_session_id,
                    state=PluginSyncOperationState.PENDING,
                    items=request.items,
                    results=None,
                    accepted=None,
                    restart_required=request.restart_after,
                    error_code=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    def transition(
        self,
        sync_id: BusinessId,
        target: PluginSyncOperationState,
    ) -> AgentPluginSyncOperationRecord:
        with self._uow_factory() as uow:
            record = self._require(uow, sync_id)
            if target not in _OPERATION_TRANSITIONS[record.state]:
                raise InvalidPluginSyncTransition(f"非法同步状态迁移: {record.state.value} -> {target.value}")
            return uow.agent_plugin_sync_operations.update(replace(record, state=target))

    def record_result(
        self,
        result: PluginSyncResult,
    ) -> AgentPluginSyncOperationRecord:
        target = (
            PluginSyncOperationState.SUCCEEDED
            if result.accepted
            else PluginSyncOperationState.FAILED
        )
        with self._uow_factory() as uow:
            record = self._require(uow, result.sync_id)
            if record.node_id != result.node_id:
                raise ValueError("同步结果节点与操作不一致")
            if target not in _OPERATION_TRANSITIONS[record.state]:
                raise InvalidPluginSyncTransition(
                    f"非法同步结果状态迁移: {record.state.value} -> {target.value}"
                )
            return uow.agent_plugin_sync_operations.update(
                replace(
                    record,
                    state=target,
                    results=result.items,
                    accepted=result.accepted,
                    restart_required=result.restart_required,
                    error_code=None,
                    completed_at=datetime.now(UTC),
                )
            )

    @staticmethod
    def _validate_items(uow: UnitOfWork, items: tuple[PluginSyncItem, ...]) -> None:
        for item in items:
            if item.action is PluginSyncAction.REMOVE:
                continue
            if item.package is None:
                raise ValueError("安装类同步项必须包含 package")
            plugin = uow.plugin_versions.get(item.plugin_id, item.version)
            if plugin is None or plugin.status is PluginStatus.REMOVED:
                raise ValueError(f"同步插件版本不可用: {item.plugin_id.root}@{item.version.root}")
            if plugin.archive_sha256 != item.package.archive_sha256:
                raise ValueError("同步包 SHA-256 与 Master 记录不一致")

    @staticmethod
    def _require(uow: UnitOfWork, sync_id: BusinessId) -> AgentPluginSyncOperationRecord:
        record = uow.agent_plugin_sync_operations.get(sync_id)
        if record is None:
            raise KeyError(f"同步操作不存在: {sync_id.root}")
        return record
