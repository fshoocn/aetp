"""v1 API 项目权限策略。

技术依赖注入位于 dependencies.py；本文件只定义平台角色和项目角色校验。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status

from master.api.v1.dependencies import CurrentUser, ProjectMemberServiceDep
from master.domain.models import User
from master.domain.enums import PlatformRole, ProjectRole

logger = logging.getLogger(__name__)


def require_platform_admin(current_user: CurrentUser) -> User:
    """要求当前用户具有平台管理员角色。"""
    if current_user.platform_role != PlatformRole.ADMIN:
        logger.warning(
            "平台管理员权限拒绝: user_id=%s, role=%s",
            current_user.id,
            current_user.platform_role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅平台管理员可执行此操作",
        )
    return current_user


PlatformAdminDep = Annotated[User, Depends(require_platform_admin)]


@dataclass(frozen=True)
class ProjectAccess:
    """当前用户在指定项目中的访问身份。"""

    user: User
    project_role: ProjectRole | None
    is_platform_admin: bool


def get_project_access(
    project_id: str,
    current_user: CurrentUser,
    member_service: ProjectMemberServiceDep,
) -> ProjectAccess:
    """解析项目成员角色；普通用户对非成员项目统一返回 404。"""
    if current_user.platform_role == PlatformRole.ADMIN:
        return ProjectAccess(current_user, None, True)
    project_role = member_service.get_role(project_id, current_user.id)
    if project_role is None:
        logger.warning(
            "项目访问拒绝: user_id=%s, project_id=%s",
            current_user.id,
            project_id,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    logger.debug(
        "项目访问通过: user_id=%s, project_id=%s, role=%s",
        current_user.id,
        project_id,
        project_role,
    )
    return ProjectAccess(current_user, project_role, False)


ProjectAccessDep = Annotated[ProjectAccess, Depends(get_project_access)]


def require_project_manager(access: ProjectAccessDep) -> ProjectAccess:
    """要求项目 maintainer/owner 或平台管理员。"""
    if access.is_platform_admin:
        return access
    if access.project_role not in (ProjectRole.MAINTAINER, ProjectRole.OWNER):
        logger.warning(
            "项目管理权限拒绝: user_id=%s, role=%s",
            access.user.id,
            access.project_role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要项目 maintainer 或 owner 权限",
        )
    return access


def require_project_operator(access: ProjectAccessDep) -> ProjectAccess:
    """要求项目 operator/maintainer/owner 或平台管理员。"""
    if access.is_platform_admin:
        return access
    if access.project_role not in (
        ProjectRole.OPERATOR,
        ProjectRole.MAINTAINER,
        ProjectRole.OWNER,
    ):
        logger.warning(
            "任务下发权限拒绝: user_id=%s, role=%s",
            access.user.id,
            access.project_role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要项目 operator、maintainer 或 owner 权限",
        )
    return access


ProjectManagerDep = Annotated[ProjectAccess, Depends(require_project_manager)]
ProjectOperatorDep = Annotated[ProjectAccess, Depends(require_project_operator)]
