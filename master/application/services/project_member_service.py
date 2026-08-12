"""项目成员授权业务服务。"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.exc import IntegrityError

from master.application.errors import (
    InactiveUserError,
    InvalidRoleGrantError,
    LastOwnerError,
    MemberAlreadyExistsError,
    MemberNotFoundError,
    ProjectNotFoundError,
)
from master.domain.enums import AccountStatus, ProjectRole
from master.domain.models import ProjectMember, ProjectMemberWithUser
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

_ROLE_RANK: dict[ProjectRole, int] = {
    ProjectRole.VIEWER: 1,
    ProjectRole.OPERATOR: 2,
    ProjectRole.MAINTAINER: 3,
    ProjectRole.OWNER: 4,
}
logger = logging.getLogger(__name__)


class ProjectMemberService:
    """负责项目成员查询、添加、角色变更和移除。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def get_role(self, project_id: str, user_id: int) -> ProjectRole | None:
        """查询用户在项目中的角色；不存在项目或成员时返回 None。"""
        with self._uow_factory() as uow:
            role = uow.members.get_role(project_id, user_id)
        result = ProjectRole(role) if role is not None else None
        logger.debug(
            "查询项目成员角色: project_id=%s, user_id=%s, role=%s",
            project_id,
            user_id,
            result,
        )
        return result

    def list_members(self, project_id: str) -> list[ProjectMemberWithUser]:
        """列出项目成员（含用户名/昵称，一次 join 取出）。"""
        with self._uow_factory() as uow:
            self._require_project(uow, project_id)
            members = uow.members.list_with_users(project_id)
            logger.debug(
                "查询项目成员: project_id=%s, count=%s",
                project_id,
                len(members),
            )
            return members

    def add_member(
        self,
        project_id: str,
        *,
        user_id: int,
        project_role: ProjectRole,
        assigned_by: int,
        actor_role: ProjectRole | None,
        is_platform_admin: bool = False,
    ) -> ProjectMemberWithUser:
        """添加项目成员并校验角色授予权限。"""
        with self._uow_factory() as uow:
            self._require_project(uow, project_id)
            self._require_role_grant(
                actor_role, project_role, is_platform_admin=is_platform_admin
            )
            target_user = uow.users.get_by_id(user_id)
            if (
                target_user is None
                or target_user.account_status != AccountStatus.ACTIVE
            ):
                raise InactiveUserError("目标用户不存在或账户未激活")

            if (
                uow.members.get_by_project_and_user(project_id, user_id)
                is not None
            ):
                raise MemberAlreadyExistsError("用户已经是项目成员")

            now = utcnow()
            member = ProjectMember(
                id=None,
                project_id=project_id,
                user_id=user_id,
                project_role=project_role,
                assigned_by=assigned_by,
                created_at=now,
                updated_at=now,
            )
            try:
                uow.members.add(member)
            except IntegrityError as exc:
                raise MemberAlreadyExistsError("用户已经是项目成员") from exc
            logger.info(
                "项目成员添加成功: project_id=%s, user_id=%s, role=%s, assigned_by=%s",
                project_id,
                user_id,
                project_role,
                assigned_by,
            )
            return self._member_with_user(uow, project_id, user_id)

    def update_member(
        self,
        project_id: str,
        user_id: int,
        *,
        project_role: ProjectRole,
        actor_role: ProjectRole | None,
        is_platform_admin: bool = False,
        assigned_by: int,
    ) -> ProjectMemberWithUser:
        """修改成员角色并保护最后一个 owner。"""
        with self._uow_factory() as uow:
            member = uow.members.get_by_project_and_user(project_id, user_id)
            if member is None:
                raise MemberNotFoundError("项目成员不存在")
            current_role = member.project_role
            self._require_role_grant(
                actor_role,
                project_role,
                is_platform_admin=is_platform_admin,
                current_role=current_role,
            )
            if current_role == ProjectRole.OWNER and project_role != ProjectRole.OWNER:
                self._require_not_last_owner(uow, project_id)
            member.project_role = project_role
            member.assigned_by = assigned_by
            member.updated_at = utcnow()
            uow.members.update(member)
            logger.info(
                "项目成员角色更新成功: project_id=%s, user_id=%s, role=%s, assigned_by=%s",
                project_id,
                user_id,
                project_role,
                assigned_by,
            )
            return self._member_with_user(uow, project_id, user_id)

    def remove_member(
        self,
        project_id: str,
        user_id: int,
        *,
        actor_role: ProjectRole | None,
        is_platform_admin: bool = False,
    ) -> None:
        """移除项目成员并保护最后一个 owner。"""
        with self._uow_factory() as uow:
            member = uow.members.get_by_project_and_user(project_id, user_id)
            if member is None:
                raise MemberNotFoundError("项目成员不存在")
            current_role = member.project_role
            self._require_member_management(
                actor_role, current_role, is_platform_admin=is_platform_admin
            )
            if current_role == ProjectRole.OWNER:
                self._require_not_last_owner(uow, project_id)
            uow.members.remove(member)
            logger.info(
                "项目成员移除成功: project_id=%s, user_id=%s, role=%s",
                project_id,
                user_id,
                current_role,
            )

    # ---- 内部校验 ----

    @staticmethod
    def _require_project(uow: UnitOfWork, project_id: str) -> None:
        if uow.projects.get_by_project_id(project_id) is None:
            raise ProjectNotFoundError("项目不存在")

    @staticmethod
    def _require_not_last_owner(uow: UnitOfWork, project_id: str) -> None:
        if uow.members.count_owners(project_id) <= 1:
            raise LastOwnerError("项目至少需要保留一个 owner")

    @staticmethod
    def _require_role_grant(
        actor_role: ProjectRole | None,
        target_role: ProjectRole,
        *,
        is_platform_admin: bool,
        current_role: ProjectRole | None = None,
    ) -> None:
        """管理员可授予任意角色；项目角色只能授予严格低于自身等级的角色。"""
        if is_platform_admin:
            return
        if actor_role is None:
            raise InvalidRoleGrantError("无权授予项目角色")
        actor_rank = _ROLE_RANK[actor_role]
        # 不能授予更高角色
        if _ROLE_RANK[target_role] > actor_rank:
            raise InvalidRoleGrantError(
                f"无权授予 {target_role.value} 角色（最高可授予 {actor_role.value}）"
            )
        # 不能授予同级角色（owner 除外，owner 可以授予另一个 owner）
        if _ROLE_RANK[target_role] == actor_rank and actor_role != ProjectRole.OWNER:
            raise InvalidRoleGrantError(
                f"无权授予 {target_role.value} 角色（不能授予同级角色）"
            )
        # 不能操作同级或更高级别的成员（owner 除外）
        if (
            current_role is not None
            and _ROLE_RANK[current_role] >= actor_rank
            and actor_role != ProjectRole.OWNER
        ):
            raise InvalidRoleGrantError("不能操作同级或更高级别的成员")

    @staticmethod
    def _require_member_management(
        actor_role: ProjectRole | None,
        target_role: ProjectRole,
        *,
        is_platform_admin: bool,
    ) -> None:
        if is_platform_admin:
            return
        if actor_role is None:
            raise InvalidRoleGrantError("无权管理项目成员")
        if _ROLE_RANK[target_role] >= _ROLE_RANK[actor_role]:
            raise InvalidRoleGrantError("不能移除同级或更高级别的成员")

    @staticmethod
    def _member_with_user(
        uow: UnitOfWork, project_id: str, user_id: int
    ) -> ProjectMemberWithUser:
        """添加/更新后返回带用户信息的成员视图。"""
        for member in uow.members.list_with_users(project_id):
            if member.user_id == user_id:
                return member
        raise MemberNotFoundError("项目成员不存在")
