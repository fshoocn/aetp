"""SQLAlchemy 事件订阅仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import EventSubscription as SubORM
from master.adapters.sqlalchemy.orm import NotificationEndpoint as EndpointORM
from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.domain.models.notification import EventSubscription
from master.domain.repositories import EventSubscriptionRepository


def _to_domain(orm: SubORM) -> EventSubscription:
    return EventSubscription(
        id=orm.id,
        subscription_id=orm.subscription_id,
        project_id=orm.project.project_id if orm.project is not None else "",
        endpoint_id=orm.endpoint.endpoint_id if orm.endpoint is not None else "",
        event_types=list(orm.event_types or []),
        filter_json=dict(orm.filter_json or {}),
        throttle_policy=dict(orm.throttle_policy or {}),
        enabled=orm.enabled,
        created_by=orm.created_by,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class EventSubscriptionRepositoryImpl(EventSubscriptionRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_subscription_id(self, subscription_id: str) -> EventSubscription | None:
        orm = (
            self._s.execute(
                select(SubORM)
                .options(joinedload(SubORM.project), joinedload(SubORM.endpoint))
                .where(SubORM.subscription_id == subscription_id)
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def list_by_project(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list[EventSubscription]:
        stmt = (
            select(SubORM)
            .options(joinedload(SubORM.project), joinedload(SubORM.endpoint))
            .where(
                SubORM.project_pk == select(ProjectORM.id).where(ProjectORM.project_id == project_id).scalar_subquery()
            )
            .order_by(SubORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, subscription: EventSubscription) -> EventSubscription:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == subscription.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {subscription.project_id}")
        endpoint_pk = self._s.execute(
            select(EndpointORM.id).where(EndpointORM.endpoint_id == subscription.endpoint_id)
        ).scalar_one_or_none()
        if endpoint_pk is None:
            raise ValueError(f"通知端点不存在: {subscription.endpoint_id}")
        orm = SubORM(
            subscription_id=subscription.subscription_id,
            project_pk=project_pk,
            endpoint_pk=endpoint_pk,
            event_types=subscription.event_types,
            filter_json=subscription.filter_json,
            throttle_policy=subscription.throttle_policy,
            enabled=subscription.enabled,
            created_by=subscription.created_by or 0,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def update(self, subscription: EventSubscription) -> EventSubscription:
        orm = self._s.get(SubORM, subscription.id)
        if orm is None:
            raise ValueError(f"事件订阅不存在: id={subscription.id}")
        orm.event_types = subscription.event_types
        orm.filter_json = subscription.filter_json
        orm.throttle_policy = subscription.throttle_policy
        orm.enabled = subscription.enabled
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def delete(self, subscription_id: str) -> None:
        orm = self._s.execute(select(SubORM).where(SubORM.subscription_id == subscription_id)).scalars().one_or_none()
        if orm is None:
            raise ValueError(f"事件订阅不存在: {subscription_id}")
        self._s.delete(orm)
        self._s.flush()
