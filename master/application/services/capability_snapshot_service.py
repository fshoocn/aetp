"""Master  节点能力快照投影服务。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from aetp_protocol.capabilities import NodeCapabilitySnapshot
from aetp_protocol.ids import BusinessId, SessionId, Sha256
from aetp_protocol.payloads import DiagnosticsSnapshot, RemoteOperationStatus

from master.domain.models import (
    AgentDiagnosticsSnapshotRecord,
    NodeCapabilitySnapshotRecord,
)
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow


class CapabilitySnapshotRejected(ValueError):
    """能力快照来自旧 session 或旧 revision。"""


class DiagnosticsSnapshotRejected(ValueError):
    """诊断快照来自旧 session 或 request_id 冲突。"""


class NodeCapabilityRevisionCache:
    """Master 进程内最新快照缓存，按 session/revision 拒绝回退。"""

    def __init__(self) -> None:
        self._records: dict[str, NodeCapabilitySnapshotRecord] = {}

    def get(self, node_id: BusinessId) -> NodeCapabilitySnapshotRecord | None:
        return self._records.get(node_id.root)

    def put_if_newer(self, record: NodeCapabilitySnapshotRecord) -> bool:
        current = self._records.get(record.node_id.root)
        if current is not None and current.session_id == record.session_id and record.revision <= current.revision:
            return False
        self._records[record.node_id.root] = record
        return True

    def invalidate(self, node_id: BusinessId) -> None:
        self._records.pop(node_id.root, None)


def snapshot_sha256(snapshot: NodeCapabilitySnapshot) -> Sha256:
    """计算能力快照的确定性 SHA-256 摘要。"""
    payload = json.dumps(
        snapshot.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return Sha256(hashlib.sha256(payload).hexdigest())


class CapabilitySnapshotProjectionService:
    """校验当前节点 session 并保存不可变能力快照。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        cache: NodeCapabilityRevisionCache | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._cache = cache or NodeCapabilityRevisionCache()

    def accept(
        self,
        snapshot: NodeCapabilitySnapshot,
        *,
        sender_session_id: SessionId | None = None,
    ) -> bool:
        if sender_session_id is not None and snapshot.session_id != sender_session_id:
            raise CapabilitySnapshotRejected("能力快照 session 与 Envelope sender 不一致")
        accepted = False
        record: NodeCapabilitySnapshotRecord | None = None
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(snapshot.node_id.root)
            if node is None:
                raise CapabilitySnapshotRejected(f"能力快照节点未注册: {snapshot.node_id.root}")
            if node.id is None:
                raise CapabilitySnapshotRejected("能力快照节点缺少内部标识")
            current = uow.node_sessions.get_current(node.id)
            if current is None or current.session_id != snapshot.session_id.root:
                raise CapabilitySnapshotRejected(
                    f"能力快照来自非当前 session: node={snapshot.node_id.root}"
                )
            record = NodeCapabilitySnapshotRecord(
                id=None,
                node_id=snapshot.node_id,
                session_id=snapshot.session_id,
                revision=snapshot.revision,
                snapshot_sha256=snapshot_sha256(snapshot),
                snapshot=snapshot,
                reported_at=snapshot.reported_at,
                created_at=utcnow(),
            )
            accepted = uow.node_capability_snapshots.add_if_newer(record)
            if accepted:
                node.online = True
                node.last_seen_at = utcnow()
                uow.nodes.save(node)
        if accepted and record is not None:
            self._cache.put_if_newer(record)
        return accepted

    def latest(self, node_id: BusinessId) -> NodeCapabilitySnapshotRecord | None:
        cached = self._cache.get(node_id)
        if cached is not None:
            return cached
        with self._uow_factory() as uow:
            record = uow.node_capability_snapshots.get_latest(node_id)
        if record is not None:
            self._cache.put_if_newer(record)
        return record

    def history(
        self,
        node_id: BusinessId,
        *,
        limit: int = 100,
    ) -> list[NodeCapabilitySnapshotRecord]:
        with self._uow_factory() as uow:
            return uow.node_capability_snapshots.list_by_node(node_id, limit=limit)


class DiagnosticsSnapshotProjectionService:
    """校验当前节点 session 并保存不可变诊断快照。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def accept(
        self,
        snapshot: DiagnosticsSnapshot,
        *,
        sender_session_id: SessionId,
    ) -> bool:
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(snapshot.node_id.root)
            if node is None or node.id is None:
                raise DiagnosticsSnapshotRejected(f"诊断快照节点未注册: {snapshot.node_id.root}")
            current = uow.node_sessions.get_current(node.id)
            if current is None or current.session_id != sender_session_id.root:
                raise DiagnosticsSnapshotRejected(
                    f"诊断快照来自非当前 session: node={snapshot.node_id.root}"
                )
            existing = uow.agent_diagnostics_snapshots.get_by_request_id(snapshot.request_id)
            if existing is not None:
                if (
                    existing.node_id != snapshot.node_id
                    or existing.session_id != sender_session_id
                    or existing.snapshot != snapshot
                ):
                    raise DiagnosticsSnapshotRejected("诊断 request_id 已用于不同快照")
                return False
            uow.agent_diagnostics_snapshots.add(
                AgentDiagnosticsSnapshotRecord(
                    id=None,
                    request_id=snapshot.request_id,
                    node_id=snapshot.node_id,
                    session_id=sender_session_id,
                    snapshot=snapshot,
                    collected_at=snapshot.collected_at,
                    created_at=datetime.now(UTC),
                )
            )
            try:
                operation_id = BusinessId(snapshot.request_id.root)
            except ValueError:
                operation_id = None
            if operation_id is not None:
                operation = uow.remote_operations.get(operation_id)
                if operation is not None:
                    if (
                        operation.kind != "diagnostics"
                        or operation.node_id != snapshot.node_id
                        or operation.expected_session_id != sender_session_id
                    ):
                        raise DiagnosticsSnapshotRejected("诊断操作节点或 session 不一致")
                    if operation.status not in {
                        RemoteOperationStatus.SUCCEEDED,
                        RemoteOperationStatus.FAILED,
                        RemoteOperationStatus.CANCELLED,
                    }:
                        uow.remote_operations.update(
                            replace(
                                operation,
                                status=RemoteOperationStatus.SUCCEEDED,
                                error_code=None,
                                message="诊断快照已采集",
                            )
                        )
            node.online = True
            node.last_seen_at = datetime.now(UTC)
            uow.nodes.save(node)
            return True

    def latest(self, node_id: BusinessId) -> AgentDiagnosticsSnapshotRecord | None:
        with self._uow_factory() as uow:
            return uow.agent_diagnostics_snapshots.get_latest(node_id)
