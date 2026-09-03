""" ResourceLease 仓储实现和条件更新。"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from aetp_protocol.execution import LeaseState, ResourceLease
from aetp_protocol.ids import BusinessId
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import ResourceLease as LeaseORM
from master.domain.models import ResourceLeaseRecord
from master.domain.repositories import ResourceLeaseRepository


def _to_domain(orm: LeaseORM) -> ResourceLeaseRecord:
    return ResourceLeaseRecord(
        id=orm.id,
        lease=ResourceLease(
            lease_id=BusinessId(orm.lease_id),
            run_id=BusinessId(orm.run_id),
            shard_id=BusinessId(orm.shard_id),
            attempt_id=BusinessId(orm.attempt_id),
            node_id=BusinessId(orm.node_id),
            resource_id=BusinessId(orm.resource_id),
            state=LeaseState(orm.state),
            revision=orm.revision,
            acquired_at=orm.acquired_at,
            heartbeat_at=orm.heartbeat_at,
            expires_at=orm.expires_at,
        ),
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class ResourceLeaseRepositoryImpl(ResourceLeaseRepository):
    """以 SQL 条件更新实现 Lease 续租、释放和过期回收。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_lease_id(self, lease_id: BusinessId) -> ResourceLeaseRecord | None:
        orm = self._s.execute(
            select(LeaseORM).where(LeaseORM.lease_id == lease_id.root)
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def get_active_by_resource(self, resource_id: BusinessId) -> ResourceLeaseRecord | None:
        orm = self._s.execute(
            select(LeaseORM).where(
                LeaseORM.resource_id == resource_id.root,
                LeaseORM.state == LeaseState.ACTIVE.value,
            )
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list_by_attempt(self, attempt_id: BusinessId) -> list[ResourceLeaseRecord]:
        statement = (
            select(LeaseORM)
            .where(LeaseORM.attempt_id == attempt_id.root)
            .order_by(LeaseORM.id)
        )
        return [_to_domain(item) for item in self._s.execute(statement).scalars().all()]

    def add(self, record: ResourceLeaseRecord) -> ResourceLeaseRecord:
        lease = record.lease
        orm = LeaseORM(
            lease_id=lease.lease_id.root,
            run_id=lease.run_id.root,
            shard_id=lease.shard_id.root,
            attempt_id=lease.attempt_id.root,
            node_id=lease.node_id.root,
            resource_id=lease.resource_id.root,
            state=lease.state.value,
            revision=lease.revision,
            acquired_at=lease.acquired_at,
            heartbeat_at=lease.heartbeat_at,
            expires_at=lease.expires_at,
            created_at=lease.acquired_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def renew(
        self,
        lease_id: BusinessId,
        *,
        expected_revision: int,
        requested_expires_at: datetime,
        now: datetime,
    ) -> ResourceLeaseRecord | None:
        orm = self._s.execute(
            select(LeaseORM).where(LeaseORM.lease_id == lease_id.root)
        ).scalar_one_or_none()
        if orm is None:
            return None
        result = cast(
            CursorResult[object],
            self._s.execute(
                update(LeaseORM)
                .where(
                    LeaseORM.id == orm.id,
                    LeaseORM.state == LeaseState.ACTIVE.value,
                    LeaseORM.revision == expected_revision,
                    LeaseORM.expires_at > now,
                )
                .values(
                    revision=LeaseORM.revision + 1,
                    heartbeat_at=now,
                    expires_at=requested_expires_at,
                )
            ),
        )
        if result.rowcount != 1:
            return None
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def release(
        self,
        lease_id: BusinessId,
        *,
        now: datetime,
        expected_revision: int | None = None,
    ) -> ResourceLeaseRecord | None:
        orm = self._s.execute(
            select(LeaseORM).where(LeaseORM.lease_id == lease_id.root)
        ).scalar_one_or_none()
        if orm is None:
            return None
        conditions = [
            LeaseORM.id == orm.id,
            LeaseORM.state == LeaseState.ACTIVE.value,
        ]
        if expected_revision is not None:
            conditions.append(LeaseORM.revision == expected_revision)
        result = cast(
            CursorResult[object],
            self._s.execute(
                update(LeaseORM)
                .where(*conditions)
                .values(
                    state=LeaseState.RELEASED.value,
                    heartbeat_at=now,
                )
            ),
        )
        if result.rowcount != 1:
            return None
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def expire_due(self, *, now: datetime) -> list[ResourceLeaseRecord]:
        statement = select(LeaseORM).where(
            LeaseORM.state == LeaseState.ACTIVE.value,
            LeaseORM.expires_at <= now,
        )
        candidates = self._s.execute(statement).scalars().all()
        expired: list[ResourceLeaseRecord] = []
        for orm in candidates:
            result = cast(
                CursorResult[object],
                self._s.execute(
                    update(LeaseORM)
                    .where(
                        LeaseORM.id == orm.id,
                        LeaseORM.state == LeaseState.ACTIVE.value,
                        LeaseORM.expires_at <= now,
                    )
                    .values(
                        state=LeaseState.EXPIRED.value,
                        heartbeat_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                continue
            self._s.flush()
            self._s.refresh(orm)
            expired.append(_to_domain(orm))
        return expired


__all__ = ["ResourceLeaseRepositoryImpl"]
