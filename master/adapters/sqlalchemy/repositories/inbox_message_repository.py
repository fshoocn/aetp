"""SQLAlchemy 入站消息去重仓储实现（P3.5，inbox_messages 表）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import InboxMessage as InboxMessageORM
from master.domain.models import InboxMessage
from master.domain.repositories import InboxMessageRepository
from master.domain.time import utcnow


def _to_domain(orm: InboxMessageORM) -> InboxMessage:
    return InboxMessage(
        id=orm.id,
        origin_id=orm.origin_id,
        message_id=orm.message_id,
        message_type=orm.message_type,
        payload_hash=orm.payload_hash,
        received_at=orm.received_at,
        processed_at=orm.processed_at,
        created_at=orm.created_at,
    )


class InboxMessageRepositoryImpl(InboxMessageRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_origin_message(
        self, origin_id: str, message_id: str
    ) -> InboxMessage | None:
        orm = self._s.execute(
            select(InboxMessageORM).where(
                InboxMessageORM.origin_id == origin_id,
                InboxMessageORM.message_id == message_id,
            )
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def add(self, message: InboxMessage) -> InboxMessage:
        """幂等去重：同 (origin_id, message_id) 已存在则直接返回已存在记录。"""
        existing = self.get_by_origin_message(message.origin_id, message.message_id)
        if existing is not None:
            return existing
        orm = InboxMessageORM(
            origin_id=message.origin_id,
            message_id=message.message_id,
            message_type=message.message_type,
            payload_hash=message.payload_hash,
            received_at=message.received_at,
            processed_at=message.processed_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def mark_processed(self, message: InboxMessage) -> InboxMessage:
        orm = self._s.get(InboxMessageORM, message.id)
        if orm is None:
            raise ValueError(f"Inbox 消息不存在: id={message.id}")
        orm.processed_at = utcnow()
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def list_unprocessed(self, *, limit: int = 100) -> list[InboxMessage]:
        stmt = (
            select(InboxMessageORM)
            .where(InboxMessageORM.processed_at.is_(None))
            .order_by(InboxMessageORM.id)
            .limit(limit)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]
