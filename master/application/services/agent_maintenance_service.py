"""Master Agent 远程维护操作、维护锁和命令编排。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from aetp_protocol.ids import BusinessId, MessageId, PluginId, SessionId, TraceId, new_id, stable_id
from aetp_protocol.logs import LogLevel
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    LogLevelUpdateRequest,
    LogLevelUpdateResult,
    MaintenanceDrainRequest,
    MaintenanceDrainResult,
    MaintenanceRestartRequest,
    MaintenanceRestartResult,
    RemoteOperationStatus,
)
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from master.domain.enums import OutboxStatus
from master.domain.models import (
    AgentPluginSyncOperationRecord,
    AuditLog,
    NodeMaintenanceLockRecord,
    OutboxMessage,
    PluginSyncOperationState,
    RemoteOperationRecord,
)
from master.domain.repositories import UnitOfWork


class AgentOfflineForMaintenance(ValueError):
    """Agent 当前没有可用 session。"""


class MaintenanceLockConflict(ValueError):
    """节点已被另一个维护操作锁定。"""


class AgentMaintenanceService:
    """创建、下发和收敛 Agent 远程维护操作。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        master_id: str = "aetp-master",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._master_id = master_id
        self._now = now or (lambda: datetime.now(UTC))

    def request_log_level(
        self,
        node_id: BusinessId,
        *,
        component: str,
        level: LogLevel,
        plugin_id: PluginId | None = None,
        expires_at: datetime | None = None,
        actor_id: int | None = None,
    ) -> RemoteOperationRecord:
        operation_id = BusinessId(new_id())
        with self._uow_factory() as uow:
            session_id = self._require_session(uow, node_id)
            request = LogLevelUpdateRequest(
                node_id=node_id,
                operation_id=operation_id,
                expected_session_id=session_id,
                component=component,
                plugin_id=plugin_id,
                level=level,
                expires_at=expires_at,
            )
            return self._create_operation(
                uow,
                operation_id=operation_id,
                node_id=node_id,
                session_id=session_id,
                kind="log_level",
                request=request.model_dump(mode="json"),
                message_type=MessageType.AGENT_LOG_LEVEL_UPDATE,
                topic_segment="agent.log.level.update",
                actor_id=actor_id,
                audit_action="agent.maintenance.log_level",
                audit_detail={
                    "component": component,
                    "plugin_id": plugin_id.root if plugin_id is not None else None,
                    "level": level.value,
                    "expires_at": expires_at.isoformat() if expires_at is not None else None,
                },
            )

    def request_drain(
        self,
        node_id: BusinessId,
        *,
        drain_timeout_s: int = 1800,
        reason: str = "",
        actor_id: int | None = None,
    ) -> RemoteOperationRecord:
        return self._request_drain_or_restart(
            node_id,
            drain_timeout_s=drain_timeout_s,
            reason=reason,
            restart=False,
            actor_id=actor_id,
        )

    def request_restart(
        self,
        node_id: BusinessId,
        *,
        drain_timeout_s: int = 1800,
        reason: str = "",
        actor_id: int | None = None,
    ) -> RemoteOperationRecord:
        return self._request_drain_or_restart(
            node_id,
            drain_timeout_s=drain_timeout_s,
            reason=reason,
            restart=True,
            actor_id=actor_id,
        )

    def _request_drain_or_restart(
        self,
        node_id: BusinessId,
        *,
        drain_timeout_s: int,
        reason: str,
        restart: bool,
        actor_id: int | None,
    ) -> RemoteOperationRecord:
        if drain_timeout_s < 0:
            raise ValueError("drain_timeout_s 不能小于 0")
        operation_id = BusinessId(new_id())
        with self._uow_factory() as uow:
            session_id = self._require_session(uow, node_id)
            if restart:
                request = MaintenanceRestartRequest(
                    node_id=node_id,
                    operation_id=operation_id,
                    expected_session_id=session_id,
                    drain_timeout_s=drain_timeout_s,
                    reason=reason,
                )
                kind = "restart"
                message_type = MessageType.AGENT_MAINTENANCE_RESTART
                topic_segment = "agent.maintenance.restart"
            else:
                request = MaintenanceDrainRequest(
                    node_id=node_id,
                    operation_id=operation_id,
                    expected_session_id=session_id,
                    drain_timeout_s=drain_timeout_s,
                    reason=reason,
                )
                kind = "drain"
                message_type = MessageType.AGENT_MAINTENANCE_DRAIN
                topic_segment = "agent.maintenance.drain"
            return self._create_operation(
                uow,
                operation_id=operation_id,
                node_id=node_id,
                session_id=session_id,
                kind=kind,
                request=request.model_dump(mode="json"),
                message_type=message_type,
                topic_segment=topic_segment,
                lock=True,
                actor_id=actor_id,
                audit_action=(
                    "agent.maintenance.restart"
                    if restart
                    else "agent.maintenance.drain"
                ),
                audit_detail={
                    "drain_timeout_s": drain_timeout_s,
                    "reason": reason,
                },
            )

    def get_operation(self, operation_id: BusinessId) -> RemoteOperationRecord | None:
        with self._uow_factory() as uow:
            operation = uow.remote_operations.get(operation_id)
            if operation is not None:
                return operation
            sync = uow.agent_plugin_sync_operations.get(operation_id)
            return _plugin_sync_operation_to_remote(sync) if sync is not None else None

    def list_operations(self, node_id: BusinessId, *, limit: int = 100) -> list[RemoteOperationRecord]:
        with self._uow_factory() as uow:
            if not 1 <= limit <= 1000:
                raise ValueError("维护操作 limit 必须在 1..1000 范围内")
            if uow.nodes.get_by_id(node_id.root) is None:
                raise KeyError(f"节点不存在: {node_id.root}")
            operations = uow.remote_operations.list_by_node(node_id, limit=limit)
            syncs = uow.agent_plugin_sync_operations.list_by_node(node_id)
            existing_ids = {operation.operation_id.root for operation in operations}
            operations.extend(
                _plugin_sync_operation_to_remote(sync)
                for sync in syncs
                if sync.sync_id.root not in existing_ids
            )
            operations.sort(
                key=lambda operation: operation.created_at or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )
            return operations[:limit]

    def handle_log_level_result(
        self,
        result: LogLevelUpdateResult,
        *,
        sender_session_id: SessionId,
    ) -> RemoteOperationRecord:
        return self._record_result(
            result.operation_id,
            result.node_id,
            sender_session_id,
            kind="log_level",
            accepted=result.accepted,
            error_code=result.code.root if result.code is not None else None,
            message=result.message,
        )

    def handle_drain_result(
        self,
        result: MaintenanceDrainResult,
        *,
        sender_session_id: SessionId,
    ) -> RemoteOperationRecord:
        return self._record_result(
            result.operation_id,
            result.node_id,
            sender_session_id,
            kind="drain",
            accepted=result.accepted,
            error_code=result.code.root if result.code is not None else None,
            message=result.message,
        )

    def handle_restart_result(
        self,
        result: MaintenanceRestartResult,
        *,
        sender_session_id: SessionId,
    ) -> RemoteOperationRecord:
        return self._record_result(
            result.operation_id,
            result.node_id,
            sender_session_id,
            kind="restart",
            accepted=result.accepted,
            error_code=result.code.root if result.code is not None else None,
            message=result.message,
        )

    def on_session_registered(self, node_id: BusinessId, session_id: SessionId) -> None:
        """新 session 注册后释放已完成的重启/插件同步锁。"""
        with self._uow_factory() as uow:
            lock = uow.maintenance_locks.get(node_id)
            if lock is None:
                return
            operation = uow.remote_operations.get(lock.operation_id)
            if operation is not None:
                if (
                    operation.kind == "restart"
                    and operation.status is RemoteOperationStatus.SUCCEEDED
                    and operation.expected_session_id != session_id
                ):
                    uow.maintenance_locks.release(node_id, operation.operation_id)
                return
            sync = uow.agent_plugin_sync_operations.get(lock.operation_id)
            if sync is not None:
                if (
                    sync.restart_required
                    and sync.state is PluginSyncOperationState.SUCCEEDED
                    and sync.expected_session_id != session_id
                ) or (
                    not sync.restart_required
                    and sync.state
                    in {
                        PluginSyncOperationState.SUCCEEDED,
                        PluginSyncOperationState.FAILED,
                        PluginSyncOperationState.CANCELLED,
                    }
                ):
                    uow.maintenance_locks.release(node_id, sync.sync_id)
                return
            # 清理没有对应业务记录的孤儿锁，避免永久阻塞调度。
            uow.maintenance_locks.release(node_id)

    def _record_result(
        self,
        operation_id: BusinessId,
        node_id: BusinessId,
        sender_session_id: SessionId,
        *,
        kind: str,
        accepted: bool,
        error_code: str | None,
        message: str,
    ) -> RemoteOperationRecord:
        with self._uow_factory() as uow:
            operation = uow.remote_operations.get(operation_id)
            if operation is None:
                raise KeyError(f"远程操作不存在: {operation_id.root}")
            if operation.node_id != node_id or operation.kind != kind:
                raise ValueError("远程操作节点或类型不一致")
            if operation.expected_session_id != sender_session_id:
                raise ValueError("远程操作结果来自旧 session")
            if operation.status in {
                RemoteOperationStatus.SUCCEEDED,
                RemoteOperationStatus.FAILED,
                RemoteOperationStatus.CANCELLED,
            }:
                return operation
            updated = uow.remote_operations.update(
                replace(
                    operation,
                    status=RemoteOperationStatus.SUCCEEDED if accepted else RemoteOperationStatus.FAILED,
                    error_code=error_code,
                    message=message,
                )
            )
            if kind == "drain" or (kind == "restart" and not accepted):
                uow.maintenance_locks.release(node_id, operation_id)
            return updated

    def _require_session(self, uow: UnitOfWork, node_id: BusinessId) -> SessionId:
        node = uow.nodes.get_by_id(node_id.root)
        if node is None or node.id is None:
            raise KeyError(f"节点不存在: {node_id.root}")
        session = uow.node_sessions.get_current(node.id)
        if session is None or not node.online:
            raise AgentOfflineForMaintenance(f"节点当前离线: {node_id.root}")
        return SessionId(session.session_id)

    def _create_operation(
        self,
        uow: UnitOfWork,
        *,
        operation_id: BusinessId,
        node_id: BusinessId,
        session_id: SessionId,
        kind: str,
        request: dict,
        message_type: MessageType,
        topic_segment: str,
        lock: bool = False,
        actor_id: int | None = None,
        audit_action: str | None = None,
        audit_detail: dict | None = None,
    ) -> RemoteOperationRecord:
        if lock:
            try:
                uow.maintenance_locks.acquire(
                    NodeMaintenanceLockRecord(
                        id=None,
                        node_id=node_id,
                        operation_id=operation_id,
                        kind=kind,
                        acquired_at=self._now(),
                    )
                )
            except ValueError as exc:
                raise MaintenanceLockConflict(str(exc)) from exc
        now = self._now()
        operation = uow.remote_operations.add(
            RemoteOperationRecord(
                id=None,
                operation_id=operation_id,
                node_id=node_id,
                kind=kind,
                status=RemoteOperationStatus.PENDING,
                expected_session_id=session_id,
                request=request,
                error_code=None,
                message="",
                created_at=now,
                updated_at=now,
            )
        )
        envelope = V2Envelope(
            message_id=MessageId(new_id()),
            sent_at=now,
            sender=V2Sender(
                kind="master",
                id=stable_id(self._master_id),
                session_id=SessionId(stable_id(f"{self._master_id}:session").root),
            ),
            message_type=message_type.value,
            trace_id=TraceId(new_id()),
            payload=request,
        )
        uow.outbox_messages.enqueue(
            OutboxMessage(
                outbox_id=stable_id(f"agent-maintenance:{operation_id.root}").root,
                aggregate_type="agent_maintenance",
                aggregate_id=operation_id.root,
                topic=v2_command_topic(node_id.root, topic_segment),
                payload=envelope.model_dump(mode="json"),
                qos=1,
                status=OutboxStatus.PENDING,
                attempts=0,
                next_attempt_at=None,
            )
        )
        if audit_action is not None:
            uow.audit_logs.add(
                AuditLog(
                    audit_id=new_id(),
                    actor_id=actor_id,
                    action=audit_action,
                    resource_type="node",
                    resource_id=node_id.root,
                    detail={
                        "operation_id": operation_id.root,
                        **(audit_detail or {}),
                    },
                    occurred_at=now,
                )
            )
        return operation


__all__ = [
    "AgentMaintenanceService",
    "AgentOfflineForMaintenance",
    "MaintenanceLockConflict",
]


def _plugin_sync_operation_to_remote(
    sync: AgentPluginSyncOperationRecord,
) -> RemoteOperationRecord:
    status = {
        PluginSyncOperationState.PENDING: RemoteOperationStatus.PENDING,
        PluginSyncOperationState.DRAINING: RemoteOperationStatus.RUNNING,
        PluginSyncOperationState.INSTALLING: RemoteOperationStatus.RUNNING,
        PluginSyncOperationState.RESTARTING: RemoteOperationStatus.RUNNING,
        PluginSyncOperationState.SUCCEEDED: RemoteOperationStatus.SUCCEEDED,
        PluginSyncOperationState.FAILED: RemoteOperationStatus.FAILED,
        PluginSyncOperationState.CANCELLED: RemoteOperationStatus.CANCELLED,
    }[sync.state]
    return RemoteOperationRecord(
        id=sync.id,
        operation_id=sync.sync_id,
        node_id=sync.node_id,
        kind="plugin_sync",
        status=status,
        expected_session_id=sync.expected_session_id,
        request={
            "sync_id": sync.sync_id.root,
            "node_id": sync.node_id.root,
            "expected_session_id": sync.expected_session_id.root,
            "items": [item.model_dump(mode="json") for item in sync.items],
            "restart_after": sync.restart_required,
        },
        error_code=sync.error_code.root if sync.error_code is not None else None,
        message=(
            "插件同步完成"
            if sync.state is PluginSyncOperationState.SUCCEEDED
            else "插件同步失败"
            if sync.state is PluginSyncOperationState.FAILED
            else "插件同步进行中"
        ),
        created_at=sync.created_at,
        updated_at=sync.updated_at,
    )
