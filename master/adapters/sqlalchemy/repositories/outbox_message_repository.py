"""SQLAlchemy 事务性 outbox 仓储实现（P3.5，outbox_messages 表）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import OutboxMessage as OutboxMessageORM
from master.domain.enums import OutboxStatus
from master.domain.models import OutboxMessage
from master.domain.repositories import OutboxMessageRepository
from master.domain.time import utcnow


def _to_domain(orm: OutboxMessageORM) -> OutboxMessage:
    return OutboxMessage(
        id=orm.id,
        outbox_id=orm.outbox_id,
        aggregate_type=orm.aggregate_type,
        aggregate_id=orm.aggregate_id,
        topic=orm.topic,
        payload=dict(orm.payload or {}),
        qos=orm.qos,
        status=OutboxStatus(orm.status),
        attempts=orm.attempts,
        next_attempt_at=orm.next_attempt_at,
        sent_at=orm.sent_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class OutboxMessageRepositoryImpl(OutboxMessageRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def enqueue(self, message: OutboxMessage) -> OutboxMessage:
        orm = OutboxMessageORM(
            outbox_id=message.outbox_id,
            aggregate_type=message.aggregate_type,
            aggregate_id=message.aggregate_id,
            topic=message.topic,
            payload=message.payload,
            qos=message.qos,
            status=message.status.value,
            attempts=message.attempts,
            next_attempt_at=message.next_attempt_at,
            sent_at=message.sent_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_outbox_id(self, outbox_id: str) -> OutboxMessage | None:
        orm = self._s.execute(
            select(OutboxMessageORM).where(
                OutboxMessageORM.outbox_id == outbox_id
            )
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def claim_due(
        self, *, limit: int = 100, now: datetime | None = None
    ) -> list[OutboxMessage]:
        """取到期消息并标记 sending（事务性 claim，防止并发重复发送）。

        条件：status IN (pending, retrying) 且 (next_attempt_at 为空或已到期)。
        """
        now = now or utcnow()
        orms = self._s.execute(
            select(OutboxMessageORM)
            .where(
                OutboxMessageORM.status.in_(
                    [OutboxStatus.PENDING.value, OutboxStatus.RETRYING.value]
                ),
                or_(
                    OutboxMessageORM.next_attempt_at.is_(None),
                    OutboxMessageORM.next_attempt_at <= now,
                ),
            )
            .order_by(OutboxMessageORM.id)
            .limit(limit)
        ).scalars().all()
        for orm in orms:
            orm.status = OutboxStatus.SENDING.value
            orm.attempts = orm.attempts + 1
            orm.sent_at = now
        self._s.flush()
        return [_to_domain(o) for o in orms]

    def update(self, message: OutboxMessage) -> OutboxMessage:
        orm = self._s.get(OutboxMessageORM, message.id)
        if orm is None:
            raise ValueError(f"Outbox 消息不存在: id={message.id}")
        orm.status = message.status.value
        orm.attempts = message.attempts
        orm.next_attempt_at = message.next_attempt_at
        orm.sent_at = message.sent_at
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
