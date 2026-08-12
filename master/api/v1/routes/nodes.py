"""v1 项目节点绑定路由。"""

from __future__ import annotations

from fastapi import APIRouter, status

from master.api.v1.permissions import ProjectAccessDep, ProjectManagerDep
from master.api.v1.schemas import (
    ProjectNodeBindingCreateRequest,
    ProjectNodeBindingOut,
    ProjectNodeBindingUpdateRequest,
)
from master.api.v1.dependencies import ProjectNodeBindingServiceDep

router = APIRouter(
    prefix="/projects/{project_id}/nodes",
    tags=["v1-project-nodes"],
)


@router.get("", response_model=list[ProjectNodeBindingOut])
def list_project_nodes(
    project_id: str,
    access: ProjectAccessDep,
    service: ProjectNodeBindingServiceDep,
) -> list[ProjectNodeBindingOut]:
    """列出项目绑定的节点；项目成员可查看。"""
    bindings = service.list_bindings(project_id)
    return [ProjectNodeBindingOut.model_validate(binding) for binding in bindings]


@router.post(
    "",
    response_model=ProjectNodeBindingOut,
    status_code=status.HTTP_201_CREATED,
)
def bind_project_node(
    project_id: str,
    body: ProjectNodeBindingCreateRequest,
    access: ProjectManagerDep,
    service: ProjectNodeBindingServiceDep,
) -> ProjectNodeBindingOut:
    """绑定节点到项目；需要 maintainer/owner 或平台管理员。"""
    binding = service.bind_node(
        project_id,
        node_id=body.node_id,
        assigned_by=access.user.persisted_id,
    )
    return ProjectNodeBindingOut.model_validate(binding)


@router.patch("/{node_id}", response_model=ProjectNodeBindingOut)
def update_project_node(
    project_id: str,
    node_id: str,
    body: ProjectNodeBindingUpdateRequest,
    access: ProjectManagerDep,
    service: ProjectNodeBindingServiceDep,
) -> ProjectNodeBindingOut:
    """启用或禁用项目节点绑定。"""
    binding = service.update_binding(
        project_id,
        node_id,
        enabled=body.enabled,
        assigned_by=access.user.persisted_id,
    )
    return ProjectNodeBindingOut.model_validate(binding)


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def unbind_project_node(
    project_id: str,
    node_id: str,
    access: ProjectManagerDep,
    service: ProjectNodeBindingServiceDep,
) -> None:
    """解除项目节点绑定。"""
    service.remove_binding(project_id, node_id)
