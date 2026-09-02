"""SQLAlchemy Agent 远程运维操作和节点维护锁仓储。"""

from __future__ import annotations

from aetp_protocol.ids import BusinessId, SessionId
from aetp_protocol.payloads import RemoteOperationStatus
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import (
    NodeMaintenanceLock as LockORM,
)
from master.adapters.sqlalchemy.orm import (
    RemoteOperation as OperationORM,
)
from master.domain.models import NodeMaintenanceLockRecord, RemoteOperationRecord
from master.domain.repositories import NodeMaintenanceLockRepository, RemoteOperationRepository


def _operation_to_domain(orm: OperationORM) -> RemoteOperationRecord:
    return RemoteOperationRecord(
        id=orm.id,
        operation_id=BusinessId(orm.operation_id),
        node_id=BusinessId(orm.node_id),
        kind=orm.kind,
        status=RemoteOperationStatus(orm.status),
        expected_session_id=(SessionId(orm.expected_session_id) if orm.expected_session_id else None),
        request=dict(orm.request or {}),
        error_code=orm.error_code,
        message=orm.message,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _lock_to_domain(orm: LockORM) -> NodeMaintenanceLockRecord:
    return NodeMaintenanceLockRecord(
        id=orm.id,
        node_id=BusinessId(orm.node_id),
        operation_id=BusinessId(orm.operation_id),
        kind=orm.kind,
        acquired_at=orm.acquired_at,
    )


class RemoteOperationRepositoryImpl(RemoteOperationRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, operation_id: BusinessId) -> RemoteOperationRecord | None:
        orm = self._s.execute(
            select(OperationORM).where(OperationORM.operation_id == operation_id.root)
        ).scalar_one_or_none()
        return _operation_to_domain(orm) if orm is not None else None

    def add(self, operation: RemoteOperationRecord) -> RemoteOperationRecord:
        orm = OperationORM(
            operation_id=operation.operation_id.root,
            node_id=operation.node_id.root,
            kind=operation.kind,
            status=operation.status.value,
            expected_session_id=(
                operation.expected_session_id.root if operation.expected_session_id is not None else None
            ),
            request=operation.request,
            error_code=operation.error_code,
            message=operation.message,
            created_at=operation.created_at,
            updated_at=operation.updated_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _operation_to_domain(orm)

    def update(self, operation: RemoteOperationRecord) -> RemoteOperationRecord:
        if operation.id is None:
            raise ValueError("更新远程操作必须包含 id")
        orm = self._s.get(OperationORM, operation.id)
        if orm is None:
            raise KeyError(f"远程操作不存在: {operation.operation_id.root}")
        orm.status = operation.status.value
        orm.error_code = operation.error_code
        orm.message = operation.message
        orm.request = operation.request
        self._s.flush()
        self._s.refresh(orm)
        return _operation_to_domain(orm)

    def list_by_node(self, node_id: BusinessId, *, limit: int = 100) -> list[RemoteOperationRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("远程操作 limit 必须在 1..1000 范围内")
        statement = (
            select(OperationORM)
            .where(OperationORM.node_id == node_id.root)
            .order_by(OperationORM.created_at.desc(), OperationORM.id.desc())
            .limit(limit)
        )
        return [_operation_to_domain(item) for item in self._s.execute(statement).scalars().all()]


class NodeMaintenanceLockRepositoryImpl(NodeMaintenanceLockRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, node_id: BusinessId) -> NodeMaintenanceLockRecord | None:
        orm = self._s.execute(
            select(LockORM).where(LockORM.node_id == node_id.root, LockORM.active.is_(True))
        ).scalar_one_or_none()
        return _lock_to_domain(orm) if orm is not None else None

    def acquire(self, lock: NodeMaintenanceLockRecord) -> NodeMaintenanceLockRecord:
        existing = self.get(lock.node_id)
        if existing is not None and existing.operation_id != lock.operation_id:
            raise ValueError(f"节点已被维护操作锁定: {lock.node_id.root}")
        if existing is not None:
            return existing
        orm = LockORM(
            node_id=lock.node_id.root,
            operation_id=lock.operation_id.root,
            kind=lock.kind,
            acquired_at=lock.acquired_at,
            active=True,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _lock_to_domain(orm)

    def release(self, node_id: BusinessId, operation_id: BusinessId | None = None) -> bool:
        statement = delete(LockORM).where(
            LockORM.node_id == node_id.root,
            LockORM.active.is_(True),
        )
        if operation_id is not None:
            statement = statement.where(LockORM.operation_id == operation_id.root)
        result = self._s.execute(statement)
        self._s.flush()
        return bool(getattr(result, "rowcount", 0))

    def is_locked(self, node_id: BusinessId) -> bool:
        return self.get(node_id) is not None


__all__ = ["NodeMaintenanceLockRepositoryImpl", "RemoteOperationRepositoryImpl"]
