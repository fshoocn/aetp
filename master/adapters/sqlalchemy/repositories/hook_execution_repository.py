"""SQLAlchemy Hook 执行审计仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import HookExecution as ExecutionORM
from master.domain.models.hook_execution import HookExecution
from master.domain.repositories import HookExecutionRepository


def _to_domain(orm: ExecutionORM) -> HookExecution:
    return HookExecution(
        id=orm.id,
        execution_id=orm.execution_id,
        event_id=orm.event_id,
        project_id=orm.project_id,
        hook_name=orm.hook_name,
        stage=orm.stage,
        status=orm.status,
        duration_ms=orm.duration_ms,
        error_message=orm.error_message,
        occurred_at=orm.occurred_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class HookExecutionRepositoryImpl(HookExecutionRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_by_project(
        self,
        project_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HookExecution]:
        stmt = select(ExecutionORM).order_by(ExecutionORM.id.desc())
        if project_id is not None:
            stmt = stmt.where(ExecutionORM.project_id == project_id)
        stmt = stmt.limit(limit).offset(offset)
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, execution: HookExecution) -> HookExecution:
        orm = ExecutionORM(
            execution_id=execution.execution_id,
            event_id=execution.event_id,
            project_id=execution.project_id,
            hook_name=execution.hook_name,
            stage=execution.stage,
            status=execution.status,
            duration_ms=execution.duration_ms,
            error_message=execution.error_message,
            occurred_at=execution.occurred_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
