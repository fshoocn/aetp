""" 项目和项目成员 API。"""

from __future__ import annotations

from datetime import datetime

from aetp_protocol.capabilities import NodeCapabilities
from aetp_protocol.ids import BusinessId
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from master.api.dependencies import (
    CurrentUser,
    IdempotencyServiceDep,
    ProjectMemberServiceDep,
    ProjectNodeBindingServiceDep,
    ProjectServiceDep,
)
from master.api.permissions import PlatformAdminDep, ProjectAccessDep, ProjectManagerDep
from master.domain.enums import PlatformRole, ProjectRole
from master.domain.models import Project, ProjectMemberWithUser, ProjectNodeBindingView

from .idempotency import complete as complete_idempotency
from .idempotency import release as release_idempotency
from .idempotency import reserve_or_replay

router = APIRouter(prefix="/api/v2/projects", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_key: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    owner_id: int | None = Field(default=None, ge=1)


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, pattern=r"^(active|archived)$")


class ProjectView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: BusinessId
    project_key: str
    name: str
    description: str
    status: str
    created_by: int
    created_at: datetime
    updated_at: datetime
    project_role: str | None = None


class ProjectMemberCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: int = Field(ge=1)
    project_role: ProjectRole


class ProjectMemberUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_role: ProjectRole


class ProjectMemberView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    project_id: BusinessId
    user_id: int
    username: str
    display_name: str
    project_role: ProjectRole
    assigned_by: int
    created_at: datetime
    updated_at: datetime


class ProjectNodeBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=26, max_length=26)


class ProjectNodeBindingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool


class ProjectDeviceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    device_id: str
    node_id: BusinessId | None
    name: str
    status: str
    online: bool
    last_seen_at: datetime | None


class ProjectNodeBindingViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    project_id: BusinessId
    node_id: BusinessId
    name: str
    hostname: str
    status: str
    online: bool
    node_enabled: bool
    enabled: bool
    assigned_by: int
    created_at: datetime
    updated_at: datetime
    capabilities: NodeCapabilities
    plugin_versions: dict[str, str]
    resource_occupancy: dict[str, str]
    devices: tuple[ProjectDeviceView, ...]


def _project_view(project: Project) -> ProjectView:
    return ProjectView(
        project_id=BusinessId(project.project_id),
        project_key=project.project_key,
        name=project.name,
        description=project.description,
        status=project.status.value,
        created_by=project.created_by,
        created_at=project.created_at,
        updated_at=project.updated_at,
        project_role=project.project_role.value if project.project_role is not None else None,
    )


def _member_view(member: ProjectMemberWithUser) -> ProjectMemberView:
    return ProjectMemberView(
        id=member.id,
        project_id=BusinessId(member.project_id),
        user_id=member.user_id,
        username=member.username,
        display_name=member.display_name,
        project_role=member.project_role,
        assigned_by=member.assigned_by,
        created_at=member.created_at,
        updated_at=member.updated_at,
    )


def _binding_view(binding: ProjectNodeBindingView) -> ProjectNodeBindingViewModel:
    return ProjectNodeBindingViewModel(
        id=binding.id,
        project_id=BusinessId(binding.project_id),
        node_id=BusinessId(binding.node_id),
        name=binding.name,
        hostname=binding.hostname,
        status=binding.status,
        online=binding.online,
        node_enabled=binding.node_enabled,
        enabled=binding.enabled,
        assigned_by=binding.assigned_by,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
        capabilities=binding.capabilities,
        plugin_versions=dict(binding.plugin_versions),
        resource_occupancy=dict(binding.resource_occupancy),
        devices=tuple(
            ProjectDeviceView(
                id=device.id or 0,
                device_id=device.device_id,
                node_id=BusinessId(device.node_id) if device.node_id else None,
                name=device.name,
                status=device.status.value,
                online=device.online,
                last_seen_at=device.last_seen_at,
            )
            for device in binding.devices
        ),
    )


def _project_id(value: str) -> BusinessId:
    try:
        return BusinessId(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" 项目 ID 不合法") from exc


@router.get("", response_model=list[ProjectView])
def list_projects(service: ProjectServiceDep, current_user: CurrentUser) -> list[ProjectView]:
    if current_user.platform_role is PlatformRole.ADMIN:
        projects = service.list_all()
    else:
        projects = service.list_visible_to_user(current_user.persisted_id)
    return [_project_view(project) for project in projects]


@router.post("", response_model=ProjectView, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreateRequest,
    service: ProjectServiceDep,
    admin: PlatformAdminDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProjectView:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"project.create:{admin.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ProjectView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        response = _project_view(
            service.create(
                project_key=body.project_key,
                name=body.name,
                description=body.description,
                created_by=admin.persisted_id,
                owner_id=body.owner_id,
            )
        )
        complete_idempotency(
            idempotency,
            result.reservation,
            response,
            response_status=status.HTTP_201_CREATED,
        )
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.get("/{project_id}", response_model=ProjectView)
def get_project(project_id: str, service: ProjectServiceDep, current_user: CurrentUser) -> ProjectView:
    typed_project_id = _project_id(project_id)
    if current_user.platform_role is PlatformRole.ADMIN:
        project = service.get_by_project_id(typed_project_id.root)
    else:
        project = service.get_visible_to_user(typed_project_id.root, current_user.persisted_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return _project_view(project)


@router.patch("/{project_id}", response_model=ProjectView)
def update_project(
    project_id: str,
    body: ProjectUpdateRequest,
    service: ProjectServiceDep,
    access: ProjectManagerDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProjectView:
    typed_project_id = _project_id(project_id)
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"project.update:{typed_project_id.root}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ProjectView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        project = service.update(
            typed_project_id.root,
            name=body.name,
            description=body.description,
            status=body.status,
        )
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        response = _project_view(project)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_200_OK)
        return response
    except HTTPException:
        release_idempotency(idempotency, result.reservation)
        raise
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.get("/{project_id}/members", response_model=list[ProjectMemberView])
def list_members(
    project_id: str,
    _access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> list[ProjectMemberView]:
    _project_id(project_id)
    return [_member_view(member) for member in service.list_members(project_id)]


@router.get(
    "/{project_id}/nodes",
    response_model=list[ProjectNodeBindingViewModel],
)
def list_nodes(
    project_id: str,
    _access: ProjectAccessDep,
    service: ProjectNodeBindingServiceDep,
) -> list[ProjectNodeBindingViewModel]:
    _project_id(project_id)
    return [_binding_view(item) for item in service.list_bindings(project_id)]


@router.post(
    "/{project_id}/nodes",
    response_model=ProjectNodeBindingViewModel,
    status_code=status.HTTP_201_CREATED,
)
def bind_node(
    project_id: str,
    body: ProjectNodeBindRequest,
    access: ProjectManagerDep,
    service: ProjectNodeBindingServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProjectNodeBindingViewModel:
    typed_project_id = _project_id(project_id)
    node_id = body.node_id
    try:
        BusinessId(node_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" 节点 ID 不合法") from exc
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"project.node.bind:{typed_project_id.root}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ProjectNodeBindingViewModel,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        response = _binding_view(
            service.bind_node(
                typed_project_id.root,
                node_id=node_id,
                assigned_by=access.user.persisted_id,
            )
        )
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_201_CREATED)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.patch(
    "/{project_id}/nodes/{node_id}",
    response_model=ProjectNodeBindingViewModel,
)
def update_node(
    project_id: str,
    node_id: str,
    body: ProjectNodeBindingUpdateRequest,
    access: ProjectManagerDep,
    service: ProjectNodeBindingServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProjectNodeBindingViewModel:
    typed_project_id = _project_id(project_id)
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"project.node.update:{typed_project_id.root}:{node_id}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ProjectNodeBindingViewModel,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        response = _binding_view(
            service.update_binding(
                typed_project_id.root,
                node_id,
                enabled=body.enabled,
                assigned_by=access.user.persisted_id,
            )
        )
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_200_OK)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.delete("/{project_id}/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_node(
    project_id: str,
    node_id: str,
    access: ProjectManagerDep,
    service: ProjectNodeBindingServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    typed_project_id = _project_id(project_id)
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"project.node.remove:{typed_project_id.root}:{node_id}:{access.user.persisted_id}",
        payload={"node_id": node_id, "operation": "remove"},
        response_model=None,
    )
    if result.replayed:
        return
    try:
        service.remove_binding(typed_project_id.root, node_id)
        complete_idempotency(idempotency, result.reservation, {}, response_status=status.HTTP_204_NO_CONTENT)
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.post("/{project_id}/members", response_model=ProjectMemberView, status_code=status.HTTP_201_CREATED)
def add_member(
    project_id: str,
    body: ProjectMemberCreateRequest,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProjectMemberView:
    typed_project_id = _project_id(project_id)
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"project.member.add:{typed_project_id.root}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ProjectMemberView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        member = service.add_member(
            typed_project_id.root,
            user_id=body.user_id,
            project_role=body.project_role,
            assigned_by=access.user.persisted_id,
            actor_role=access.project_role,
            is_platform_admin=access.is_platform_admin,
        )
        response = _member_view(member)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_201_CREATED)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.patch("/{project_id}/members/{user_id}", response_model=ProjectMemberView)
def update_member(
    project_id: str,
    user_id: int,
    body: ProjectMemberUpdateRequest,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProjectMemberView:
    typed_project_id = _project_id(project_id)
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"project.member.update:{typed_project_id.root}:{user_id}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ProjectMemberView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        member = service.update_member(
            typed_project_id.root,
            user_id,
            project_role=body.project_role,
            actor_role=access.project_role,
            is_platform_admin=access.is_platform_admin,
            assigned_by=access.user.persisted_id,
        )
        response = _member_view(member)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_200_OK)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    project_id: str,
    user_id: int,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    typed_project_id = _project_id(project_id)
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"project.member.remove:{typed_project_id.root}:{user_id}:{access.user.persisted_id}",
        payload={"user_id": user_id, "operation": "remove"},
        response_model=None,
    )
    if result.replayed:
        return
    try:
        service.remove_member(
            typed_project_id.root,
            user_id,
            actor_role=access.project_role,
            is_platform_admin=access.is_platform_admin,
            assigned_by=access.user.persisted_id,
        )
        complete_idempotency(idempotency, result.reservation, {}, response_status=status.HTTP_204_NO_CONTENT)
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


__all__ = ["router"]
