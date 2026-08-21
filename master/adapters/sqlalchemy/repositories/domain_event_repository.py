"""SQLAlchemy 不可变领域事件仓储实现（P3.5，domain_events 表）。

sequence 在 add 时分配 MAX+1，全局单调唯一，保证事件顺序。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import DomainEvent as DomainEventORM
from master.domain.models import DomainEvent
from master.domain.repositories import DomainEventRepository


def _to_domain(orm: DomainEventORM) -> DomainEvent:
    return DomainEvent(
        id=orm.id,
        event_id=orm.event_id,
        sequence=orm.sequence,
        project_id=orm.project_id,
        event_type=orm.event_type,
        aggregate_id=orm.aggregate_id,
        payload=dict(orm.payload or {}),
        occurred_at=orm.occurred_at,
        created_at=orm.created_at,
    )


class DomainEventRepositoryImpl(DomainEventRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, event: DomainEvent) -> DomainEvent:
        max_seq = self._s.execute(select(func.max(DomainEventORM.sequence))).scalar_one_or_none()
        sequence = (max_seq or 0) + 1
        orm = DomainEventORM(
            event_id=event.event_id,
            sequence=sequence,
            project_id=event.project_id,
            event_type=event.event_type,
            aggregate_id=event.aggregate_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_event_id(self, event_id: str) -> DomainEvent | None:
        orm = self._s.execute(select(DomainEventORM).where(DomainEventORM.event_id == event_id)).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list(
        self,
        *,
        project_id: str | None = None,
        after_sequence: int | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        stmt = select(DomainEventORM)
        if project_id is not None:
            stmt = stmt.where(DomainEventORM.project_id == project_id)
        if after_sequence is not None:
            stmt = stmt.where(DomainEventORM.sequence > after_sequence)
        stmt = stmt.order_by(DomainEventORM.sequence).limit(limit)
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]
