"""SQLAlchemy case 级结果仓储实现（P3.4，run_case_results 表，D-20）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import RunCaseResult as RunCaseResultORM
from master.adapters.sqlalchemy.orm import RunShard as RunShardORM
from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM
from master.domain.enums import CaseStatus
from master.domain.models import RunCaseResult
from master.domain.repositories import RunCaseResultRepository


def _to_domain(orm: RunCaseResultORM) -> RunCaseResult:
    return RunCaseResult(
        id=orm.id,
        run_id=orm.run.run_id if orm.run is not None else "",
        shard_id=orm.shard.shard_id if orm.shard is not None else "",
        case_key=orm.case_key,
        attempt_no=orm.attempt_no,
        sequence=orm.sequence,
        status=CaseStatus(orm.status),
        duration_ms=orm.duration_ms,
        error_summary=orm.error_summary,
        detail=dict(orm.detail) if orm.detail else None,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class RunCaseResultRepositoryImpl(RunCaseResultRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add_many(self, results: list[RunCaseResult]) -> list[RunCaseResult]:
        run_pk: int | None = None
        shard_pk: int | None = None
        first_run_id = results[0].run_id if results else ""
        for result in results:
            if run_pk is None or result.run_id != first_run_id:
                run_pk = self._s.execute(
                    select(TaskRunORM.id).where(TaskRunORM.run_id == result.run_id)
                ).scalar_one_or_none()
            if run_pk is None:
                raise ValueError(f"Run 不存在: {result.run_id}")
            shard_pk = self._s.execute(
                select(RunShardORM.id).where(RunShardORM.shard_id == result.shard_id)
            ).scalar_one_or_none()
            if shard_pk is None:
                raise ValueError(f"Shard 不存在: {result.shard_id}")
            orm = RunCaseResultORM(
                run_pk=run_pk,
                shard_pk=shard_pk,
                case_key=result.case_key,
                attempt_no=result.attempt_no,
                sequence=result.sequence,
                status=result.status.value,
                duration_ms=result.duration_ms,
                error_summary=result.error_summary,
                detail=result.detail,
            )
            self._s.add(orm)
        self._s.flush()
        return [self.get_by_key(r.run_id, r.shard_id, r.case_key, r.attempt_no) or r for r in results]

    def list_by_run(self, run_id: str) -> list[RunCaseResult]:
        stmt = (
            select(RunCaseResultORM)
            .options(
                joinedload(RunCaseResultORM.run),
                joinedload(RunCaseResultORM.shard),
            )
            .where(
                RunCaseResultORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery()
            )
            .order_by(RunCaseResultORM.case_key, RunCaseResultORM.attempt_no)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def list_by_shard(self, run_id: str, shard_id: str) -> list[RunCaseResult]:
        stmt = (
            select(RunCaseResultORM)
            .options(
                joinedload(RunCaseResultORM.run),
                joinedload(RunCaseResultORM.shard),
            )
            .where(
                RunCaseResultORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery(),
                RunCaseResultORM.shard_pk
                == select(RunShardORM.id).where(RunShardORM.shard_id == shard_id).scalar_subquery(),
            )
            .order_by(RunCaseResultORM.case_key, RunCaseResultORM.attempt_no)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def get_by_key(self, run_id: str, shard_id: str, case_key: str, attempt_no: int) -> RunCaseResult | None:
        orm = (
            self._s.execute(
                select(RunCaseResultORM)
                .options(
                    joinedload(RunCaseResultORM.run),
                    joinedload(RunCaseResultORM.shard),
                )
                .where(
                    RunCaseResultORM.run_pk
                    == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery(),
                    RunCaseResultORM.shard_pk
                    == select(RunShardORM.id).where(RunShardORM.shard_id == shard_id).scalar_subquery(),
                    RunCaseResultORM.case_key == case_key,
                    RunCaseResultORM.attempt_no == attempt_no,
                )
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def update(self, result: RunCaseResult) -> RunCaseResult:
        orm = self._s.get(RunCaseResultORM, result.id)
        if orm is None:
            raise ValueError(f"case 结果不存在: id={result.id}")
        orm.status = result.status.value
        orm.sequence = result.sequence
        orm.duration_ms = result.duration_ms
        orm.error_summary = result.error_summary
        orm.detail = result.detail
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
