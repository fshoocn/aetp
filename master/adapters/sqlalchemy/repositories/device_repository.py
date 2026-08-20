"""SQLAlchemy 设备仓储实现。"""

from __future__ import annotations

from aetp_protocol.capabilities import PhysicalDeviceCapability
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
        capability=PhysicalDeviceCapability.model_validate(
            orm.capabilities or {"resource_type": "generic"}
        ),
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

    def add(self, device: Device) -> Device:
        node_pk = None
        if device.node_id is not None:
            node_pk = self._s.execute(
                select(NodeORM.id).where(NodeORM.node_id == device.node_id)
            ).scalar_one_or_none()
            if node_pk is None:
                raise ValueError(f"节点不存在: {device.node_id}")
        orm = DeviceORM(
            device_id=device.device_id,
            node_pk=node_pk,
            name=device.name,
            capabilities=device.capability.model_dump(
                mode="json", exclude_none=True
            ),
            status=device.status.value,
            online=device.online,
            last_seen_at=device.last_seen_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def update(self, device: Device) -> Device:
        orm = self._s.get(DeviceORM, device.id)
        if orm is None:
            raise ValueError(f"设备不存在: id={device.id}")
        orm.name = device.name
        orm.capabilities = device.capability.model_dump(
            mode="json", exclude_none=True
        )
        orm.status = device.status.value
        orm.online = device.online
        orm.last_seen_at = device.last_seen_at
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

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
