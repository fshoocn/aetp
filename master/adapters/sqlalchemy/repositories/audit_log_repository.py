"""SQLAlchemy 审计日志仓储实现（P3.5，audit_logs 表，append-only）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import AuditLog as AuditLogORM
from master.domain.models import AuditLog
from master.domain.repositories import AuditLogRepository


def _to_domain(orm: AuditLogORM) -> AuditLog:
    return AuditLog(
        id=orm.id,
        audit_id=orm.audit_id,
        project_id=orm.project_id,
        actor_id=orm.actor_id,
        action=orm.action,
        resource_type=orm.resource_type,
        resource_id=orm.resource_id,
        request_id=orm.request_id,
        detail=dict(orm.detail or {}),
        occurred_at=orm.occurred_at,
        created_at=orm.created_at,
    )


class AuditLogRepositoryImpl(AuditLogRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, log: AuditLog) -> AuditLog:
        orm = AuditLogORM(
            audit_id=log.audit_id,
            project_id=log.project_id,
            actor_id=log.actor_id,
            action=log.action,
            resource_type=log.resource_type,
            resource_id=log.resource_id,
            request_id=log.request_id,
            detail=log.detail,
            occurred_at=log.occurred_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_audit_id(self, audit_id: str) -> AuditLog | None:
        orm = self._s.execute(select(AuditLogORM).where(AuditLogORM.audit_id == audit_id)).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list(
        self,
        *,
        project_id: str | None = None,
        actor_id: int | None = None,
        action: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        stmt = select(AuditLogORM)
        if project_id is not None:
            stmt = stmt.where(AuditLogORM.project_id == project_id)
        if actor_id is not None:
            stmt = stmt.where(AuditLogORM.actor_id == actor_id)
        if action is not None:
            stmt = stmt.where(AuditLogORM.action == action)
        stmt = stmt.order_by(AuditLogORM.occurred_at.desc(), AuditLogORM.id.desc()).limit(limit).offset(offset)
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]
