"""项目 CRUD 业务服务。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aetp_protocol.ids import new_id
from sqlalchemy.exc import IntegrityError

from master.application.errors import (
    InvalidProjectOwnerError,
    ProjectKeyAlreadyExistsError,
)
from master.domain.enums import ProjectRole, ProjectStatus
from master.domain.models import Project, ProjectMember
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)


class ProjectService:
    """负责项目创建、查询和元数据修改。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def create(
        self,
        *,
        project_key: str,
        name: str,
        description: str,
        created_by: int,
        owner_id: int | None = None,
    ) -> Project:
        """创建项目并建立首个 owner 成员关系（同一事务）。"""
        normalized_key = project_key.strip()
        normalized_name = name.strip()
        if not normalized_key:
            raise InvalidProjectOwnerError("项目标识不能为空")
        if not normalized_name:
            raise InvalidProjectOwnerError("项目名称不能为空")

        try:
            with self._uow_factory() as uow:
                owner_user_id = owner_id if owner_id is not None else created_by
                owner = uow.users.get_by_id(owner_user_id)
                if owner is None or not owner.is_active:
                    raise InvalidProjectOwnerError("项目 owner 不存在或账户未激活")
                if uow.projects.get_by_key(normalized_key) is not None:
                    raise ProjectKeyAlreadyExistsError(f"项目标识已存在: {normalized_key}")

                now = utcnow()
                project = Project(
                    id=None,
                    project_id=new_id(),
                    project_key=normalized_key,
                    name=normalized_name,
                    description=description,
                    status=ProjectStatus.ACTIVE,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
                created = uow.projects.add(project)
                uow.members.add(
                    ProjectMember(
                        id=None,
                        project_id=created.project_id,
                        user_id=owner_user_id,
                        project_role=ProjectRole.OWNER,
                        assigned_by=created_by,
                        created_at=now,
                        updated_at=now,
                    )
                )
                logger.info(
                    "项目创建成功: project_id=%s, project_key=%s, created_by=%s, owner_id=%s",
                    created.project_id,
                    created.project_key,
                    created_by,
                    owner_user_id,
                )
                return created
        except IntegrityError as exc:
            raise ProjectKeyAlreadyExistsError(f"项目标识已存在: {normalized_key}") from exc

    def list_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Project]:
        """查询全部项目；调用方负责先完成平台或项目权限校验。"""
        with self._uow_factory() as uow:
            projects = uow.projects.list_all(limit=limit, offset=offset)
            logger.debug("查询全部项目: count=%s", len(projects))
            return projects

    def list_visible_to_user(self, user_id: int, limit: int = 100, offset: int = 0) -> list[Project]:
        """按项目成员关系查询用户有权限查看的项目。"""
        with self._uow_factory() as uow:
            projects = uow.projects.list_visible_to_user(user_id, limit=limit, offset=offset)
            logger.debug("查询用户可见项目: user_id=%s, count=%s", user_id, len(projects))
            return projects

    def get_by_project_id(self, project_id: str) -> Project | None:
        """按公开 project_id 查询；调用方负责先完成权限校验。"""
        with self._uow_factory() as uow:
            project = uow.projects.get_by_project_id(project_id)
            logger.debug(
                "查询项目详情: project_id=%s, found=%s",
                project_id,
                project is not None,
            )
            return project

    def get_visible_to_user(self, project_id: str, user_id: int) -> Project | None:
        """按项目成员关系查询项目详情。"""
        with self._uow_factory() as uow:
            project = uow.projects.get_by_project_id(project_id)
            if project is None:
                return None
            role = uow.members.get_role(project_id, user_id)
            if role is None:
                return None
            project.project_role = ProjectRole(role)
            logger.debug(
                "查询项目成员详情: project_id=%s, user_id=%s, found=%s",
                project_id,
                user_id,
                project is not None,
            )
            return project

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> Project | None:
        """修改项目元数据；project_key 和 created_by 不可修改。"""
        with self._uow_factory() as uow:
            project = uow.projects.get_by_project_id(project_id)
            if project is None:
                return None
            if name is not None:
                normalized_name = name.strip()
                if not normalized_name:
                    raise InvalidProjectOwnerError("项目名称不能为空")
                project.name = normalized_name
            if description is not None:
                project.description = description
            if status is not None:
                project.status = ProjectStatus(status)
            project.updated_at = utcnow()
            updated = uow.projects.update(project)
            logger.info(
                "项目更新成功: project_id=%s, name=%s, status=%s",
                project_id,
                updated.name,
                updated.status,
            )
            return updated
