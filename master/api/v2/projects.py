"""V2 项目和项目成员 API。"""

from __future__ import annotations

from datetime import datetime

from aetp_protocol.ids import BusinessId
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from master.api.v1.dependencies import (
    CurrentUser,
    ProjectMemberServiceDep,
    ProjectServiceDep,
)
from master.api.v1.permissions import PlatformAdminDep, ProjectManagerDep
from master.domain.enums import PlatformRole, ProjectRole
from master.domain.models import Project, ProjectMemberWithUser

router = APIRouter(prefix="/api/v2/projects", tags=["v2-projects"])


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


def _project_id(value: str) -> BusinessId:
    try:
        return BusinessId(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="V2 项目 ID 不合法") from exc


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
) -> ProjectView:
    try:
        return _project_view(
            service.create(
                project_key=body.project_key,
                name=body.name,
                description=body.description,
                created_by=admin.persisted_id,
                owner_id=body.owner_id,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    _access: ProjectManagerDep,
) -> ProjectView:
    typed_project_id = _project_id(project_id)
    project = service.update(
        typed_project_id.root,
        name=body.name,
        description=body.description,
        status=body.status,
    )
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return _project_view(project)


@router.get("/{project_id}/members", response_model=list[ProjectMemberView])
def list_members(
    project_id: str,
    _access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> list[ProjectMemberView]:
    _project_id(project_id)
    return [_member_view(member) for member in service.list_members(project_id)]


@router.post("/{project_id}/members", response_model=ProjectMemberView, status_code=status.HTTP_201_CREATED)
def add_member(
    project_id: str,
    body: ProjectMemberCreateRequest,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> ProjectMemberView:
    _project_id(project_id)
    try:
        member = service.add_member(
            project_id,
            user_id=body.user_id,
            project_role=body.project_role,
            assigned_by=access.user.persisted_id,
            actor_role=access.project_role,
            is_platform_admin=access.is_platform_admin,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _member_view(member)


@router.patch("/{project_id}/members/{user_id}", response_model=ProjectMemberView)
def update_member(
    project_id: str,
    user_id: int,
    body: ProjectMemberUpdateRequest,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> ProjectMemberView:
    _project_id(project_id)
    try:
        member = service.update_member(
            project_id,
            user_id,
            project_role=body.project_role,
            actor_role=access.project_role,
            is_platform_admin=access.is_platform_admin,
            assigned_by=access.user.persisted_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _member_view(member)


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    project_id: str,
    user_id: int,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> None:
    _project_id(project_id)
    try:
        service.remove_member(
            project_id,
            user_id,
            actor_role=access.project_role,
            is_platform_admin=access.is_platform_admin,
            assigned_by=access.user.persisted_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["router"]
