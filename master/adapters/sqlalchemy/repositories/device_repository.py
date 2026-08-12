"""SQLAlchemy 设备仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import Device as DeviceORM
from master.adapters.sqlalchemy.orm import Node as NodeORM
from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import ProjectNodeBinding as BindingORM
from master.domain.enums import DeviceStatus
from master.domain.models import Device
from master.domain.repositories import DeviceRepository


def _to_domain(orm: DeviceORM) -> Device:
    return Device(
        id=orm.id,
        device_id=orm.device_id,
        node_id=orm.node.node_id if orm.node is not None else None,
        name=orm.name,
        status=DeviceStatus(orm.status),
        online=orm.online,
        last_seen_at=orm.last_seen_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _project_pk_subq(session: Session, project_id: str):
    return (
        select(ProjectORM.id)
        .where(ProjectORM.project_id == project_id)
        .scalar_subquery()
    )


class DeviceRepositoryImpl(DeviceRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_all(self, *, online: bool | None = None) -> list[Device]:
        stmt = select(DeviceORM).order_by(DeviceORM.id)
        if online is not None:
            stmt = stmt.where(DeviceORM.online.is_(online))
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def get_by_id(self, device_id: str) -> Device | None:
        orm = self._s.execute(
            select(DeviceORM).where(DeviceORM.device_id == device_id)
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list_for_project(
        self, project_id: str, *, online: bool | None = None
    ) -> list[Device]:
        stmt = (
            select(DeviceORM)
            .join(NodeORM, NodeORM.id == DeviceORM.node_pk)
            .join(BindingORM, BindingORM.node_pk == NodeORM.id)
            .where(
                BindingORM.project_pk == _project_pk_subq(self._s, project_id),
                BindingORM.enabled.is_(True),
                NodeORM.enabled.is_(True),
            )
            .order_by(DeviceORM.id)
        )
        if online is not None:
            stmt = stmt.where(DeviceORM.online.is_(online))
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def get_for_project(self, project_id: str, device_id: str) -> Device | None:
        orm = self._s.execute(
            select(DeviceORM)
            .join(NodeORM, NodeORM.id == DeviceORM.node_pk)
            .join(BindingORM, BindingORM.node_pk == NodeORM.id)
            .where(
                DeviceORM.device_id == device_id,
                BindingORM.project_pk == _project_pk_subq(self._s, project_id),
                BindingORM.enabled.is_(True),
                NodeORM.enabled.is_(True),
            )
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None
