"""SQLAlchemy 节点仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from master.adapters.sqlalchemy.orm import Device as DeviceORM
from master.adapters.sqlalchemy.orm import Node as NodeORM
from master.domain.enums import NodeStatus
from master.domain.models import Device, Node
from master.domain.repositories import NodeRepository


def _device_to_domain(orm: DeviceORM) -> Device:
    return Device(
        id=orm.id,
        device_id=orm.device_id,
        node_id=orm.node.node_id if orm.node is not None else None,
        name=orm.name,
        status=orm.status,
        online=orm.online,
        last_seen_at=orm.last_seen_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _to_domain(orm: NodeORM) -> Node:
    return Node(
        id=orm.id,
        node_id=orm.node_id,
        name=orm.name,
        hostname=orm.hostname,
        status=NodeStatus(orm.status),
        online=orm.online,
        enabled=orm.enabled,
        tags=list(orm.tags or []),
        capabilities=dict(orm.capabilities or {}),
        protocol_version=orm.protocol_version,
        last_seen_at=orm.last_seen_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        devices=[_device_to_domain(d) for d in (orm.devices or [])],
    )


class NodeRepositoryImpl(NodeRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_all(
        self, *, online: bool | None = None, enabled: bool | None = None
    ) -> list[Node]:
        stmt = (
            select(NodeORM)
            .options(selectinload(NodeORM.devices))
            .order_by(NodeORM.id)
        )
        if online is not None:
            stmt = stmt.where(NodeORM.online.is_(online))
        if enabled is not None:
            stmt = stmt.where(NodeORM.enabled.is_(enabled))
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def get_by_id(self, node_id: str) -> Node | None:
        orm = self._s.execute(
            select(NodeORM)
            .options(selectinload(NodeORM.devices))
            .where(NodeORM.node_id == node_id)
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None
