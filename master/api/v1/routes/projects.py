"""v1 项目 CRUD 路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from master.api.v1.dependencies import (
    CurrentUser,
    ProjectMemberServiceDep,
    ProjectServiceDep,
)
from master.api.v1.permissions import PlatformAdminDep, ProjectManagerDep
from master.api.v1.schemas import (
    ProjectCreateRequest,
    ProjectMemberCreateRequest,
    ProjectMemberOut,
    ProjectMemberUpdateRequest,
    ProjectOut,
    ProjectUpdateRequest,
)
from master.domain.enums import PlatformRole

router = APIRouter(prefix="/projects", tags=["v1-projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(
    service: ProjectServiceDep,
    current_user: CurrentUser,
    limit: int = 100,
    offset: int = 0,
) -> list[ProjectOut]:
    """列出项目（分页）；管理员查看全部，普通用户按成员关系查看。"""
    if current_user.platform_role == PlatformRole.ADMIN:
        projects = service.list_all(limit=limit, offset=offset)
    else:
        projects = service.list_visible_to_user(
            current_user.persisted_id, limit=limit, offset=offset
        )
    return [ProjectOut.model_validate(project) for project in projects]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreateRequest,
    service: ProjectServiceDep,
    admin: PlatformAdminDep,
) -> ProjectOut:
    """创建项目；当前仅平台管理员可创建。"""
    project = service.create(
        project_key=body.project_key,
        name=body.name,
        description=body.description,
        created_by=admin.persisted_id,
        owner_id=body.owner_id,
    )
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    service: ProjectServiceDep,
    current_user: CurrentUser,
) -> ProjectOut:
    """查询项目详情；普通用户必须是项目成员。"""
    if current_user.platform_role == PlatformRole.ADMIN:
        project = service.get_by_project_id(project_id)
    else:
        project = service.get_visible_to_user(project_id, current_user.persisted_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="项目不存在",
        )
    return ProjectOut.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str,
    body: ProjectUpdateRequest,
    service: ProjectServiceDep,
    access: ProjectManagerDep,
) -> ProjectOut:
    """修改项目元数据；需要项目 maintainer/owner 或平台管理员。"""
    project = service.update(
        project_id,
        name=body.name,
        description=body.description,
        status=body.status.value if body.status is not None else None,
    )
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="项目不存在",
        )
    return ProjectOut.model_validate(project)


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
def list_project_members(
    project_id: str,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> list[ProjectMemberOut]:
    """查询项目成员；需要项目 maintainer/owner 或平台管理员。"""
    members = service.list_members(project_id)
    return [ProjectMemberOut.model_validate(member) for member in members]


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberOut,
    status_code=status.HTTP_201_CREATED,
)
def add_project_member(
    project_id: str,
    body: ProjectMemberCreateRequest,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> ProjectMemberOut:
    """添加成员；角色授予受当前项目角色层级限制。"""
    member = service.add_member(
        project_id,
        user_id=body.user_id,
        project_role=body.project_role,
        assigned_by=access.user.persisted_id,
        actor_role=access.project_role,
        is_platform_admin=access.is_platform_admin,
    )
    return ProjectMemberOut.model_validate(member)


@router.patch(
    "/{project_id}/members/{user_id}",
    response_model=ProjectMemberOut,
)
def update_project_member(
    project_id: str,
    user_id: int,
    body: ProjectMemberUpdateRequest,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> ProjectMemberOut:
    """修改成员角色并保护最后一个 owner。"""
    member = service.update_member(
        project_id,
        user_id,
        project_role=body.project_role,
        actor_role=access.project_role,
        is_platform_admin=access.is_platform_admin,
        assigned_by=access.user.persisted_id,
    )
    return ProjectMemberOut.model_validate(member)


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project_member(
    project_id: str,
    user_id: int,
    access: ProjectManagerDep,
    service: ProjectMemberServiceDep,
) -> None:
    """移除成员并保护最后一个 owner。"""
    service.remove_member(
        project_id,
        user_id,
        actor_role=access.project_role,
        is_platform_admin=access.is_platform_admin,
        assigned_by=access.user.persisted_id,
    )
