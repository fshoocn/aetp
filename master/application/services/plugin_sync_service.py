"""Master  插件期望版本和 Agent 同步操作服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.ids import BusinessId, MessageId, PluginId, SemVer, SessionId, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import MaintenanceStatus
from aetp_protocol.plugin_types import DesiredPluginVersion, PluginStatus, PluginSyncAction
from aetp_protocol.plugins import PluginSyncItem, PluginSyncRequest, PluginSyncResult
from aetp_protocol.topics import command_topic

from master.application.services.agent_maintenance_service import MaintenanceLockConflict
from master.domain.enums import OutboxStatus
from master.domain.models import (
    AgentPluginDesiredVersionRecord,
    AgentPluginSyncOperationRecord,
    AuditLog,
    NodeMaintenanceLockRecord,
    OutboxMessage,
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
        PluginSyncOperationState.SUCCEEDED,
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


class AgentOfflineForPluginSync(ValueError):
    """节点没有当前可用 session，无法下发插件同步命令。"""


class PluginSyncService:
    """维护 Master 期望版本，并持久化 Agent 同步操作事实。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        package_url_builder: Callable[[PluginId, SemVer], str] | None = None,
        master_id: str = "aetp-master",
    ) -> None:
        self._uow_factory = uow_factory
        self._package_url_builder = package_url_builder
        self._master_id = master_id

    def request(
        self,
        node_id: BusinessId,
        items: tuple[PluginSyncItem, ...],
        *,
        drain_timeout_s: int = 1800,
        restart_after: bool = True,
        actor_id: int | None = None,
    ) -> AgentPluginSyncOperationRecord:
        """根据当前节点 session 创建并下发一次插件同步请求。"""
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(node_id.root)
            if node is None or node.id is None:
                raise KeyError(f"节点不存在: {node_id.root}")
            session = uow.node_sessions.get_current(node.id)
            if session is None or not node.online:
                raise AgentOfflineForPluginSync(f"节点当前离线: {node_id.root}")
            expected_session_id = SessionId(session.session_id)
        return self.dispatch(
            PluginSyncRequest(
                sync_id=BusinessId(new_id()),
                node_id=node_id,
                expected_session_id=expected_session_id,
                items=items,
                drain_timeout_s=drain_timeout_s,
                restart_after=restart_after,
            ),
            actor_id=actor_id,
        )
    def dispatch(
        self,
        request: PluginSyncRequest,
        *,
        actor_id: int | None = None,
    ) -> AgentPluginSyncOperationRecord:
        """原子写入同步操作和  command outbox。"""
        normalized = self._with_package_urls(request)
        now = datetime.now(UTC)
        with self._uow_factory() as uow:
            existing = uow.agent_plugin_sync_operations.get(request.sync_id)
            if existing is not None:
                if existing.node_id != request.node_id or existing.items != normalized.items:
                    raise ValueError("sync_id 已用于不同的同步请求")
                return existing
            node = uow.nodes.get_by_id(request.node_id.root)
            if node is None or node.id is None:
                raise KeyError(f"节点不存在: {request.node_id.root}")
            session = uow.node_sessions.get_current(node.id)
            if session is None or not node.online:
                raise AgentOfflineForPluginSync(f"节点当前离线: {request.node_id.root}")
            if session.session_id != request.expected_session_id.root:
                raise AgentOfflineForPluginSync("节点 session 已变化，请重新生成同步请求")
            self._validate_items(uow, normalized.items)
            try:
                uow.maintenance_locks.acquire(
                    NodeMaintenanceLockRecord(
                        id=None,
                        node_id=normalized.node_id,
                        operation_id=normalized.sync_id,
                        kind="plugin_sync",
                        acquired_at=now,
                    )
                )
            except ValueError as exc:
                raise MaintenanceLockConflict(str(exc)) from exc
            record = uow.agent_plugin_sync_operations.add(
                AgentPluginSyncOperationRecord(
                    id=None,
                    sync_id=normalized.sync_id,
                    node_id=normalized.node_id,
                    expected_session_id=normalized.expected_session_id,
                    state=PluginSyncOperationState.PENDING,
                    items=normalized.items,
                    results=None,
                    accepted=None,
                    restart_required=normalized.restart_after,
                    error_code=None,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            envelope = self._build_command_envelope(normalized)
            outbox_id = stable_id(f"plugin-sync:{normalized.sync_id.root}").root
            uow.outbox_messages.enqueue(
                OutboxMessage(
                    outbox_id=outbox_id,
                    aggregate_type="agent_plugin_sync",
                    aggregate_id=normalized.sync_id.root,
                    topic=command_topic(normalized.node_id.root, "agent.plugin.sync"),
                    payload=envelope.model_dump(mode="json"),
                    qos=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=None,
                )
            )
            uow.audit_logs.add(
                AuditLog(
                    audit_id=new_id(),
                    actor_id=actor_id,
                    action="agent.maintenance.plugin_sync",
                    resource_type="node",
                    resource_id=normalized.node_id.root,
                    detail={
                        "sync_id": normalized.sync_id.root,
                        "items": [
                            {
                                "plugin_id": item.plugin_id.root,
                                "version": item.version.root,
                                "action": item.action.value,
                            }
                            for item in normalized.items
                        ],
                        "drain_timeout_s": normalized.drain_timeout_s,
                        "restart_after": normalized.restart_after,
                    },
                    occurred_at=now,
                )
            )
            return uow.agent_plugin_sync_operations.update(
                replace(record, state=PluginSyncOperationState.DRAINING)
            )

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

    def set_desired_version_for_nodes(
        self,
        node_ids: tuple[BusinessId, ...],
        desired: DesiredPluginVersion,
    ) -> tuple[AgentPluginDesiredVersionRecord, ...]:
        """为指定节点集合原子设置同一版本期望。"""
        now = datetime.now(UTC)
        with self._uow_factory() as uow:
            self._validate_desired_plugin(uow, desired)
            records: list[AgentPluginDesiredVersionRecord] = []
            unique_nodes = {node_id.root: node_id for node_id in node_ids}
            for node_id in unique_nodes.values():
                node = uow.nodes.get_by_id(node_id.root)
                if node is None:
                    raise KeyError(f"节点不存在: {node_id.root}")
                records.append(
                    uow.agent_plugin_desired_versions.upsert(
                        AgentPluginDesiredVersionRecord(
                            id=None,
                            node_id=node_id,
                            desired=desired,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                )
            return tuple(records)

    def set_desired_version_for_tag(
        self,
        tag: str,
        desired: DesiredPluginVersion,
    ) -> tuple[AgentPluginDesiredVersionRecord, ...]:
        """按节点标签选择目标节点并原子设置版本期望。"""
        normalized_tag = tag.strip()
        if not normalized_tag:
            raise ValueError("节点组标签不能为空")
        with self._uow_factory() as uow:
            self._validate_desired_plugin(uow, desired)
            nodes = tuple(
                node
                for node in uow.nodes.list_all()
                if normalized_tag in node.tags
            )
            now = datetime.now(UTC)
            return tuple(
                uow.agent_plugin_desired_versions.upsert(
                    AgentPluginDesiredVersionRecord(
                        id=None,
                        node_id=BusinessId(node.node_id),
                        desired=desired,
                        created_at=now,
                        updated_at=now,
                    )
                )
                for node in nodes
            )

    @staticmethod
    def _validate_desired_plugin(uow: UnitOfWork, desired: DesiredPluginVersion) -> None:
        plugin = uow.plugin_versions.get(desired.plugin_id, desired.version)
        if plugin is None or plugin.status is PluginStatus.REMOVED:
            raise ValueError("期望插件版本不存在或已移除")
        if plugin.point is not desired.point:
            raise ValueError("期望插件 point 与插件版本不一致")

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
        *,
        sender_session_id: SessionId | None = None,
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
            if sender_session_id is not None and record.expected_session_id != sender_session_id:
                raise ValueError("同步结果来自旧 session")
            if record.state in {
                PluginSyncOperationState.SUCCEEDED,
                PluginSyncOperationState.FAILED,
                PluginSyncOperationState.CANCELLED,
            }:
                if (
                    record.results == result.items
                    and record.accepted == result.accepted
                    and record.restart_required == result.restart_required
                ):
                    return record
                raise InvalidPluginSyncTransition("终态同步操作收到不同的重复结果")
            if target not in _OPERATION_TRANSITIONS[record.state]:
                raise InvalidPluginSyncTransition(
                    f"非法同步结果状态迁移: {record.state.value} -> {target.value}"
                )
            updated = uow.agent_plugin_sync_operations.update(
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
            if not result.accepted or not result.restart_required:
                uow.maintenance_locks.release(result.node_id, result.sync_id)
            return updated

    def record_maintenance_status(
        self,
        status: MaintenanceStatus,
        *,
        sender_session_id: SessionId,
    ) -> AgentPluginSyncOperationRecord | None:
        """按 Agent 维护状态推进对应同步操作；无 sync_id 的状态只作在线事实。"""
        if status.sync_id is None:
            return None
        targets = {
            "draining": PluginSyncOperationState.DRAINING,
            "updating": PluginSyncOperationState.INSTALLING,
            "restarting": PluginSyncOperationState.RESTARTING,
        }
        target = targets.get(status.state.value)
        if target is None:
            return None
        with self._uow_factory() as uow:
            record = self._require(uow, status.sync_id)
            if record.node_id != status.node_id:
                raise ValueError("维护状态节点与同步操作不一致")
            if record.expected_session_id != sender_session_id:
                raise ValueError("维护状态来自旧 session")
            if record.state is target:
                return record
            if record.state in {
                PluginSyncOperationState.SUCCEEDED,
                PluginSyncOperationState.FAILED,
                PluginSyncOperationState.CANCELLED,
            }:
                return record
            if target not in _OPERATION_TRANSITIONS[record.state]:
                raise InvalidPluginSyncTransition(
                    f"非法维护状态迁移: {record.state.value} -> {target.value}"
                )
            return uow.agent_plugin_sync_operations.update(replace(record, state=target))

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
            if plugin.point is not item.point:
                raise ValueError("同步插件 point 与 Master 记录不一致")
            if plugin.archive_sha256 != item.package.archive_sha256:
                raise ValueError("同步包 SHA-256 与 Master 记录不一致")

    def _with_package_urls(self, request: PluginSyncRequest) -> PluginSyncRequest:
        if self._package_url_builder is None:
            return request
        items = tuple(
            item.model_copy(
                update={
                    "package": item.package.model_copy(
                        update={
                            "download_url": self._package_url_builder(item.plugin_id, item.version),
                        }
                    )
                }
            )
            if item.package is not None and item.action is not PluginSyncAction.REMOVE
            else item
            for item in request.items
        )
        return request.model_copy(update={"items": items})

    def _build_command_envelope(self, request: PluginSyncRequest) -> Envelope:
        return Envelope(
            message_id=MessageId(new_id()),
            sent_at=datetime.now(UTC),
            sender=Sender(
                kind=SenderKind.MASTER,
                id=stable_id(self._master_id),
                session_id=SessionId(stable_id(f"{self._master_id}:session").root),
            ),
            message_type=MessageType.AGENT_PLUGIN_SYNC.value,
            trace_id=TraceId(new_id()),
            payload=request.model_dump(mode="json"),
        )

    @staticmethod
    def _require(uow: UnitOfWork, sync_id: BusinessId) -> AgentPluginSyncOperationRecord:
        record = uow.agent_plugin_sync_operations.get(sync_id)
        if record is None:
            raise KeyError(f"同步操作不存在: {sync_id.root}")
        return record
