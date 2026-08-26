"""SQLAlchemy 投递记录仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import EventDelivery as DeliveryORM
from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.domain.models.notification import EventDelivery
from master.domain.repositories import EventDeliveryRepository


def _to_domain(orm: DeliveryORM) -> EventDelivery:
    return EventDelivery(
        id=orm.id,
        delivery_id=orm.delivery_id,
        project_id=orm.project.project_id if orm.project is not None else "",
        event_id=orm.event_id,
        subscription_id=orm.subscription_id,
        endpoint_id=orm.endpoint_id,
        content=dict(orm.content or {}),
        status=orm.status,
        attempts=orm.attempts,
        next_attempt_at=orm.next_attempt_at,
        sent_at=orm.sent_at,
        response_summary=orm.response_summary,
        error_message=orm.error_message,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class EventDeliveryRepositoryImpl(EventDeliveryRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_delivery_id(self, delivery_id: str) -> EventDelivery | None:
        orm = (
            self._s.execute(
                select(DeliveryORM)
                .options(joinedload(DeliveryORM.project))
                .where(DeliveryORM.delivery_id == delivery_id)
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def list_by_project(
        self,
        project_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EventDelivery]:
        stmt = (
            select(DeliveryORM)
            .options(joinedload(DeliveryORM.project))
            .where(
                DeliveryORM.project_pk
                == select(ProjectORM.id).where(ProjectORM.project_id == project_id).scalar_subquery()
            )
            .order_by(DeliveryORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(DeliveryORM.status == status)
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, delivery: EventDelivery) -> EventDelivery:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == delivery.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {delivery.project_id}")
        orm = DeliveryORM(
            delivery_id=delivery.delivery_id,
            project_pk=project_pk,
            event_id=delivery.event_id,
            subscription_id=delivery.subscription_id,
            endpoint_id=delivery.endpoint_id,
            content=delivery.content,
            status=delivery.status,
            attempts=delivery.attempts,
            next_attempt_at=delivery.next_attempt_at,
            sent_at=delivery.sent_at,
            response_summary=delivery.response_summary,
            error_message=delivery.error_message,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_event_subscription(self, event_id: str, subscription_id: str) -> EventDelivery | None:
        orm = (
            self._s.execute(
                select(DeliveryORM)
                .options(joinedload(DeliveryORM.project))
                .where(
                    DeliveryORM.event_id == event_id,
                    DeliveryORM.subscription_id == subscription_id,
                )
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def update(self, delivery: EventDelivery) -> EventDelivery:
        orm = self._s.get(DeliveryORM, delivery.id)
        if orm is None:
            raise ValueError(f"投递记录不存在: id={delivery.id}")
        orm.status = delivery.status
        orm.attempts = delivery.attempts
        orm.next_attempt_at = delivery.next_attempt_at
        orm.sent_at = delivery.sent_at
        orm.response_summary = delivery.response_summary
        orm.error_message = delivery.error_message
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
