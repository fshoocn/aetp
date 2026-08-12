"""SQLAlchemy 项目仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import ProjectMember as ProjectMemberORM
from master.domain.enums import ProjectStatus
from master.domain.models import Project
from master.domain.repositories import ProjectRepository


def _to_domain(orm: ProjectORM) -> Project:
    return Project(
        id=orm.id,
        project_id=orm.project_id,
        project_key=orm.project_key,
        name=orm.name,
        description=orm.description,
        status=ProjectStatus(orm.status),
        created_by=orm.created_by,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class ProjectRepositoryImpl(ProjectRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_project_id(self, project_id: str) -> Project | None:
        orm = self._s.execute(
            select(ProjectORM).where(ProjectORM.project_id == project_id)
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def get_by_key(self, project_key: str) -> Project | None:
        orm = self._s.execute(
            select(ProjectORM).where(ProjectORM.project_key == project_key)
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list_all(self, *, limit: int = 100, offset: int = 0) -> list[Project]:
        stmt = (
            select(ProjectORM).order_by(ProjectORM.id.desc()).limit(limit).offset(offset)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def list_visible_to_user(
        self, user_id: int, *, limit: int = 100, offset: int = 0
    ) -> list[Project]:
        stmt = (
            select(ProjectORM)
            .join(
                ProjectMemberORM,
                ProjectMemberORM.project_pk == ProjectORM.id,
            )
            .where(ProjectMemberORM.user_id == user_id)
            .order_by(ProjectORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, project: Project) -> Project:
        orm = ProjectORM(
            project_id=project.project_id,
            project_key=project.project_key,
            name=project.name,
            description=project.description,
            status=project.status.value,
            created_by=project.created_by,
        )
        self._s.add(orm)
        self._s.flush()
        return _to_domain(orm)

    def update(self, project: Project) -> Project:
        orm = self._s.get(ProjectORM, project.id)
        if orm is None:
            raise ValueError(f"项目不存在: id={project.id}")
        orm.project_key = project.project_key
        orm.name = project.name
        orm.description = project.description
        orm.status = project.status.value
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
