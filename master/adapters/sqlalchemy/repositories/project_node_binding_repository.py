"""SQLAlchemy 项目节点绑定仓储实现。"""

from __future__ import annotations

from aetp_protocol.capabilities import PhysicalDeviceCapability
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from master.adapters.sqlalchemy.orm import Device as DeviceORM
from master.adapters.sqlalchemy.orm import Node as NodeORM
from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import ProjectNodeBinding as BindingORM
from master.domain.enums import DeviceStatus
from master.domain.models import (
    Device,
    ProjectNodeBinding,
    ProjectNodeBindingView,
)
from master.domain.repositories import ProjectNodeBindingRepository


def _device_to_domain(orm: DeviceORM) -> Device:
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


class ProjectNodeBindingRepositoryImpl(ProjectNodeBindingRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_with_nodes(self, project_id: str) -> list[ProjectNodeBindingView]:
        rows = self._s.execute(
            select(BindingORM, NodeORM)
            .join(NodeORM, NodeORM.id == BindingORM.node_pk)
            .options(selectinload(NodeORM.devices))
            .where(BindingORM.project_pk == _project_pk_subq(self._s, project_id))
            .order_by(BindingORM.id)
        ).all()
        return [self._to_view(binding, node, project_id) for binding, node in rows]

    def get(self, project_id: str, node_id: str) -> ProjectNodeBinding | None:
        orm = self._s.execute(
            select(BindingORM).where(
                BindingORM.project_pk == _project_pk_subq(self._s, project_id),
                BindingORM.node_pk
                == select(NodeORM.id).where(NodeORM.node_id == node_id).scalar_subquery(),
            )
        ).scalar_one_or_none()
        if orm is None:
            return None
        return ProjectNodeBinding(
            id=orm.id,
            project_id=project_id,
            node_id=node_id,
            enabled=orm.enabled,
            assigned_by=orm.assigned_by,
            created_at=orm.created_at,
            updated_at=orm.updated_at,
        )

    def add(self, binding: ProjectNodeBinding) -> ProjectNodeBinding:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == binding.project_id)
        ).scalar_one_or_none()
        node_pk = self._s.execute(
            select(NodeORM.id).where(NodeORM.node_id == binding.node_id)
        ).scalar_one_or_none()
        if project_pk is None or node_pk is None:
            raise ValueError("项目或节点不存在")
        orm = BindingORM(
            project_pk=project_pk,
            node_pk=node_pk,
            enabled=binding.enabled,
            assigned_by=binding.assigned_by,
        )
        self._s.add(orm)
        self._s.flush()
        return ProjectNodeBinding(
            id=orm.id,
            project_id=binding.project_id,
            node_id=binding.node_id,
            enabled=orm.enabled,
            assigned_by=orm.assigned_by,
            created_at=orm.created_at,
            updated_at=orm.updated_at,
        )

    def update(self, binding: ProjectNodeBinding) -> ProjectNodeBinding:
        orm = self._s.get(BindingORM, binding.id)
        if orm is None:
            raise ValueError(f"项目节点绑定不存在: id={binding.id}")
        orm.enabled = binding.enabled
        orm.assigned_by = binding.assigned_by
        self._s.flush()
        self._s.refresh(orm)
        return ProjectNodeBinding(
            id=orm.id,
            project_id=binding.project_id,
            node_id=binding.node_id,
            enabled=orm.enabled,
            assigned_by=orm.assigned_by,
            created_at=orm.created_at,
            updated_at=orm.updated_at,
        )

    def remove(self, binding: ProjectNodeBinding) -> None:
        orm = self._s.get(BindingORM, binding.id)
        if orm is not None:
            self._s.delete(orm)

    @staticmethod
    def _to_view(
        binding: BindingORM, node: NodeORM, project_id: str
    ) -> ProjectNodeBindingView:
        from aetp_protocol.capabilities import NodeCapabilities

        return ProjectNodeBindingView(
            id=binding.id,
            project_id=project_id,
            node_id=node.node_id,
            name=node.name,
            hostname=node.hostname,
            status=node.status,
            online=node.online,
            node_enabled=node.enabled,
            enabled=binding.enabled,
            assigned_by=binding.assigned_by,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
            capabilities=NodeCapabilities.model_validate(
                node.capabilities or {}
            ),
            plugin_versions=dict(node.plugin_versions or {}),
            devices=[_device_to_domain(d) for d in (node.devices or [])],
        )
