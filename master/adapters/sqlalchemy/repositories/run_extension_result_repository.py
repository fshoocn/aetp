"""SQLAlchemy Run Reporter/Analyzer 扩展结果仓储。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import RunExtensionResult as ExtensionORM
from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM
from master.domain.models.reporting import RunExtensionResult
from master.domain.repositories import RunExtensionResultRepository


def _to_domain(orm: ExtensionORM) -> RunExtensionResult:
    return RunExtensionResult(
        id=orm.id,
        extension_id=orm.extension_id,
        run_id=orm.run.run_id if orm.run is not None else "",
        extension_point=orm.extension_point,
        plugin_id=orm.plugin_id,
        plugin_version=orm.plugin_version,
        status=orm.status,
        result=dict(orm.result) if orm.result is not None else None,
        derived_artifact_ids=list(orm.derived_artifact_ids or []),
        error_message=orm.error_message,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class RunExtensionResultRepositoryImpl(RunExtensionResultRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(
        self,
        run_id: str,
        extension_point: str,
        plugin_id: str,
        plugin_version: str,
    ) -> RunExtensionResult | None:
        orm = (
            self._s.execute(
                select(ExtensionORM)
                .options(joinedload(ExtensionORM.run))
                .where(
                    ExtensionORM.run_pk
                    == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery(),
                    ExtensionORM.extension_point == extension_point,
                    ExtensionORM.plugin_id == plugin_id,
                    ExtensionORM.plugin_version == plugin_version,
                )
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def add(self, result: RunExtensionResult) -> RunExtensionResult:
        run_pk = self._s.execute(select(TaskRunORM.id).where(TaskRunORM.run_id == result.run_id)).scalar_one_or_none()
        if run_pk is None:
            raise ValueError(f"Run 不存在: {result.run_id}")
        orm = ExtensionORM(
            extension_id=result.extension_id,
            run_pk=run_pk,
            extension_point=result.extension_point,
            plugin_id=result.plugin_id,
            plugin_version=result.plugin_version,
            status=result.status,
            result=result.result,
            derived_artifact_ids=result.derived_artifact_ids,
            error_message=result.error_message,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def update(self, result: RunExtensionResult) -> RunExtensionResult:
        orm = self._s.get(ExtensionORM, result.id)
        if orm is None:
            raise ValueError(f"Run 扩展结果不存在: id={result.id}")
        orm.status = result.status
        orm.result = result.result
        orm.derived_artifact_ids = result.derived_artifact_ids
        orm.error_message = result.error_message
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def list_by_run(self, run_id: str) -> list[RunExtensionResult]:
        stmt = (
            select(ExtensionORM)
            .options(joinedload(ExtensionORM.run))
            .where(
                ExtensionORM.run_pk
                == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery()
            )
            .order_by(ExtensionORM.id)
        )
        return [_to_domain(orm) for orm in self._s.execute(stmt).scalars().all()]
