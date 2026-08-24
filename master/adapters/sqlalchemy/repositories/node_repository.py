"""SQLAlchemy 节点仓储实现。"""

from __future__ import annotations

from aetp_protocol.capabilities import NodeCapabilities, PhysicalDeviceCapability
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from master.adapters.sqlalchemy.orm import Device as DeviceORM
from master.adapters.sqlalchemy.orm import Node as NodeORM
from master.domain.enums import DeviceStatus, NodeStatus
from master.domain.models import Device, Node
from master.domain.repositories import NodeRepository


def _device_to_domain(orm: DeviceORM) -> Device:
    return Device(
        id=orm.id,
        device_id=orm.device_id,
        node_id=orm.node.node_id if orm.node is not None else None,
        name=orm.name,
        status=DeviceStatus(orm.status),
        online=orm.online,
        capability=PhysicalDeviceCapability.model_validate(orm.capabilities or {"resource_type": "generic"}),
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
        capabilities=NodeCapabilities.model_validate(orm.capabilities or {}),
        protocol_version=orm.protocol_version,
        plugin_versions=dict(orm.plugin_versions or {}),
        plugin_supported_versions={key: list(value) for key, value in (orm.plugin_supported_versions or {}).items()},
        last_seen_at=orm.last_seen_at,
        load=dict(orm.load or {}),
        resource_occupancy=dict(orm.resource_occupancy or {}),
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        devices=[_device_to_domain(d) for d in (orm.devices or [])],
    )


class NodeRepositoryImpl(NodeRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_all(self, *, online: bool | None = None, enabled: bool | None = None) -> list[Node]:
        stmt = select(NodeORM).options(selectinload(NodeORM.devices)).order_by(NodeORM.id)
        if online is not None:
            stmt = stmt.where(NodeORM.online.is_(online))
        if enabled is not None:
            stmt = stmt.where(NodeORM.enabled.is_(enabled))
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def get_by_id(self, node_id: str) -> Node | None:
        orm = self._s.execute(
            select(NodeORM).options(selectinload(NodeORM.devices)).where(NodeORM.node_id == node_id)
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def save(self, node: Node) -> Node:
        """创建或更新节点（按 node_id upsert，P4.4 注册/心跳投影用）。"""
        orm = self._s.execute(select(NodeORM).where(NodeORM.node_id == node.node_id)).scalar_one_or_none()
        if orm is None:
            orm = NodeORM(node_id=node.node_id)
            self._s.add(orm)
        orm.name = node.name
        orm.hostname = node.hostname
        orm.status = node.status.value
        orm.online = node.online
        orm.enabled = node.enabled
        orm.tags = node.tags
        orm.capabilities = node.capabilities.model_dump(mode="json", exclude_none=True)
        orm.protocol_version = node.protocol_version
        orm.plugin_versions = node.plugin_versions
        orm.plugin_supported_versions = node.plugin_supported_versions
        orm.last_seen_at = node.last_seen_at
        orm.load = node.load
        orm.resource_occupancy = node.resource_occupancy
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def mark_all_offline(self) -> int:
        """把所有节点投影重置为 offline（Master 启动恢复用）。

        掉线期间的 online 状态不可信，统一重置为 offline，等待 Agent
        心跳重新刷新，避免调度器在心跳到达前误判节点在线。

        Returns:
            重置的节点数量
        """
        from typing import Any

        from sqlalchemy import update as sa_update
        from sqlalchemy.engine import Result

        result: Result[Any] = self._s.execute(
            sa_update(NodeORM).values(
                online=False,
                status=NodeStatus.OFFLINE.value,
            )
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            self._s.flush()
        return count
