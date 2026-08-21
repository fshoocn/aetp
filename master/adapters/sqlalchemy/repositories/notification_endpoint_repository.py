"""SQLAlchemy 通知端点仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import NotificationEndpoint as EndpointORM
from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.domain.models.notification import NotificationEndpoint
from master.domain.repositories import NotificationEndpointRepository


def _to_domain(orm: EndpointORM) -> NotificationEndpoint:
    return NotificationEndpoint(
        id=orm.id,
        endpoint_id=orm.endpoint_id,
        project_id=orm.project.project_id if orm.project is not None else "",
        channel_type=orm.channel_type,
        name=orm.name,
        config=dict(orm.config or {}),
        secret_ref=orm.secret_ref,
        enabled=orm.enabled,
        created_by=orm.created_by,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class NotificationEndpointRepositoryImpl(NotificationEndpointRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_endpoint_id(self, endpoint_id: str) -> NotificationEndpoint | None:
        orm = (
            self._s.execute(
                select(EndpointORM)
                .options(joinedload(EndpointORM.project))
                .where(EndpointORM.endpoint_id == endpoint_id)
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def list_by_project(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list[NotificationEndpoint]:
        stmt = (
            select(EndpointORM)
            .options(joinedload(EndpointORM.project))
            .where(
                EndpointORM.project_pk
                == select(ProjectORM.id).where(ProjectORM.project_id == project_id).scalar_subquery()
            )
            .order_by(EndpointORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, endpoint: NotificationEndpoint) -> NotificationEndpoint:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == endpoint.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {endpoint.project_id}")
        orm = EndpointORM(
            endpoint_id=endpoint.endpoint_id,
            project_pk=project_pk,
            channel_type=endpoint.channel_type,
            name=endpoint.name,
            config=endpoint.config,
            secret_ref=endpoint.secret_ref,
            enabled=endpoint.enabled,
            created_by=endpoint.created_by or 0,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def update(self, endpoint: NotificationEndpoint) -> NotificationEndpoint:
        orm = self._s.get(EndpointORM, endpoint.id)
        if orm is None:
            raise ValueError(f"通知端点不存在: id={endpoint.id}")
        orm.name = endpoint.name
        orm.channel_type = endpoint.channel_type
        orm.config = endpoint.config
        orm.secret_ref = endpoint.secret_ref
        orm.enabled = endpoint.enabled
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def delete(self, endpoint_id: str) -> None:
        orm = self._s.execute(select(EndpointORM).where(EndpointORM.endpoint_id == endpoint_id)).scalars().one_or_none()
        if orm is None:
            raise ValueError(f"通知端点不存在: {endpoint_id}")
        self._s.delete(orm)
        self._s.flush()
