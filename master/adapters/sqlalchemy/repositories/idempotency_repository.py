"""SQLAlchemy 写 API 幂等键仓储。"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import IdempotencyRecord as IdempotencyORM
from master.domain.models.idempotency import IdempotencyRecord
from master.domain.repositories import IdempotencyRecordRepository


def _to_domain(orm: IdempotencyORM) -> IdempotencyRecord:
    return IdempotencyRecord(
        id=orm.id,
        key=orm.key,
        scope=orm.scope,
        request_hash=orm.request_hash,
        status=orm.status,
        response_status=orm.response_status,
        response_body=dict(orm.response_body) if orm.response_body is not None else None,
        expires_at=orm.expires_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class IdempotencyRecordRepositoryImpl(IdempotencyRecordRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        orm = (
            self._s.execute(
                select(IdempotencyORM).where(
                    IdempotencyORM.scope == scope,
                    IdempotencyORM.key == key,
                )
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def add(self, record: IdempotencyRecord) -> IdempotencyRecord:
        orm = IdempotencyORM(
            key=record.key,
            scope=record.scope,
            request_hash=record.request_hash,
            status=record.status,
            response_status=record.response_status,
            response_body=record.response_body,
            expires_at=record.expires_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def update(self, record: IdempotencyRecord) -> IdempotencyRecord:
        orm = self._s.get(IdempotencyORM, record.id)
        if orm is None:
            raise ValueError(f"幂等记录不存在: id={record.id}")
        orm.status = record.status
        orm.response_status = record.response_status
        orm.response_body = record.response_body
        orm.expires_at = record.expires_at
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def delete(self, scope: str, key: str) -> None:
        self._s.execute(
            delete(IdempotencyORM).where(
                IdempotencyORM.scope == scope,
                IdempotencyORM.key == key,
            )
        )
        self._s.flush()
