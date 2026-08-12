"""SQLAlchemy 项目成员仓储实现。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import ProjectMember as ProjectMemberORM
from master.adapters.sqlalchemy.orm import User as UserORM
from master.domain.enums import ProjectRole
from master.domain.models import ProjectMember, ProjectMemberWithUser
from master.domain.repositories import ProjectMemberRepository


def _project_pk_subq(session: Session, project_id: str):
    """将业务 project_id 解析为代理主键子查询（保持查询形状简单）。"""
    return (
        select(ProjectORM.id)
        .where(ProjectORM.project_id == project_id)
        .scalar_subquery()
    )


def _to_domain(orm: ProjectMemberORM, project_id: str) -> ProjectMember:
    return ProjectMember(
        id=orm.id,
        project_id=project_id,
        user_id=orm.user_id,
        project_role=ProjectRole(orm.project_role),
        assigned_by=orm.assigned_by,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class ProjectMemberRepositoryImpl(ProjectMemberRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_role(self, project_id: str, user_id: int) -> str | None:
        return self._s.execute(
            select(ProjectMemberORM.project_role)
            .where(
                ProjectMemberORM.project_pk == _project_pk_subq(self._s, project_id),
                ProjectMemberORM.user_id == user_id,
            )
        ).scalar_one_or_none()

    def list_with_users(self, project_id: str) -> list[ProjectMemberWithUser]:
        rows = self._s.execute(
            select(ProjectMemberORM, UserORM)
            .join(UserORM, UserORM.id == ProjectMemberORM.user_id)
            .where(ProjectMemberORM.project_pk == _project_pk_subq(self._s, project_id))
            .order_by(ProjectMemberORM.id)
        ).all()
        return [
            ProjectMemberWithUser(
                id=member.id,
                project_id=project_id,
                user_id=member.user_id,
                username=user.username,
                display_name=user.display_name,
                project_role=ProjectRole(member.project_role),
                assigned_by=member.assigned_by,
                created_at=member.created_at,
                updated_at=member.updated_at,
            )
            for member, user in rows
        ]

    def get_by_project_and_user(
        self, project_id: str, user_id: int
    ) -> ProjectMember | None:
        orm = self._s.execute(
            select(ProjectMemberORM).where(
                ProjectMemberORM.project_pk == _project_pk_subq(self._s, project_id),
                ProjectMemberORM.user_id == user_id,
            )
        ).scalar_one_or_none()
        return _to_domain(orm, project_id) if orm is not None else None

    def count_owners(self, project_id: str) -> int:
        return self._s.execute(
            select(func.count())
            .select_from(ProjectMemberORM)
            .where(
                ProjectMemberORM.project_pk == _project_pk_subq(self._s, project_id),
                ProjectMemberORM.project_role == ProjectRole.OWNER.value,
            )
        ).scalar_one()

    def add(self, member: ProjectMember) -> ProjectMember:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(
                ProjectORM.project_id == member.project_id
            )
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {member.project_id}")
        orm = ProjectMemberORM(
            project_pk=project_pk,
            user_id=member.user_id,
            project_role=member.project_role.value,
            assigned_by=member.assigned_by,
        )
        self._s.add(orm)
        self._s.flush()
        return _to_domain(orm, member.project_id)

    def update(self, member: ProjectMember) -> ProjectMember:
        orm = self._s.get(ProjectMemberORM, member.id)
        if orm is None:
            raise ValueError(f"项目成员不存在: id={member.id}")
        orm.project_role = member.project_role.value
        orm.assigned_by = member.assigned_by
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm, member.project_id)

    def remove(self, member: ProjectMember) -> None:
        orm = self._s.get(ProjectMemberORM, member.id)
        if orm is not None:
            self._s.delete(orm)
