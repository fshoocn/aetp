"""Master V2 插件期望版本和 Agent 同步操作服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from aetp_protocol.ids import BusinessId, MessageId, PluginId, SemVer, SessionId, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import MaintenanceStatus
from aetp_protocol.plugin_types import DesiredPluginVersion, PluginStatus, PluginSyncAction
from aetp_protocol.plugins import PluginSyncItem, PluginSyncRequest, PluginSyncResult
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from master.domain.enums import OutboxStatus
from master.domain.models import (
    AgentPluginDesiredVersionRecord,
    AgentPluginSyncOperationRecord,
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
            )
        )

    def dispatch(self, request: PluginSyncRequest) -> AgentPluginSyncOperationRecord:
        """原子写入同步操作和 V2 command outbox。"""
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
                    topic=v2_command_topic(normalized.node_id.root, "agent.plugin.sync"),
                    payload=envelope.model_dump(mode="json"),
                    qos=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=None,
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

    def _build_command_envelope(self, request: PluginSyncRequest) -> V2Envelope:
        return V2Envelope(
            message_id=MessageId(new_id()),
            sent_at=datetime.now(UTC),
            sender=V2Sender(
                kind="master",
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
