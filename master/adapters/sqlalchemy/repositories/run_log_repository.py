"""SQLAlchemy Run 执行日志仓储实现（run_logs 表）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import RunLog as RunLogORM
from master.adapters.sqlalchemy.orm import RunShard as RunShardORM
from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM
from master.domain.enums import RunLogLevel
from master.domain.models import RunLog
from master.domain.repositories import RunLogRepository


def _to_domain(orm: RunLogORM) -> RunLog:
    return RunLog(
        id=orm.id,
        run_id=orm.run.run_id if orm.run is not None else "",
        shard_id=orm.shard.shard_id if orm.shard is not None else None,
        attempt_id=orm.attempt_id,
        plan_id=orm.plan_id or "",
        node_id=orm.node_id,
        sequence=orm.sequence,
        level=RunLogLevel(orm.level),
        message=orm.message,
        detail=dict(orm.detail) if orm.detail else None,
        occurred_at=orm.occurred_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class RunLogRepositoryImpl(RunLogRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, log: RunLog) -> RunLog:
        run_pk = self._s.execute(select(TaskRunORM.id).where(TaskRunORM.run_id == log.run_id)).scalar_one_or_none()
        if run_pk is None:
            raise ValueError(f"Run 不存在: {log.run_id}")
        shard_pk = None
        if log.shard_id is not None:
            shard_pk = self._s.execute(
                select(RunShardORM.id).where(RunShardORM.shard_id == log.shard_id)
            ).scalar_one_or_none()
        orm = RunLogORM(
            run_pk=run_pk,
            shard_pk=shard_pk,
            node_id=log.node_id,
            attempt_id=log.attempt_id,
            plan_id=log.plan_id or None,
            sequence=log.sequence,
            level=log.level.value,
            message=log.message,
            detail=log.detail,
            occurred_at=log.occurred_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def add_many(self, logs: list[RunLog]) -> list[RunLog]:
        """批量插入；(run_pk, sequence) 冲突由调用方（投影服务）做幂等跳过。"""
        persisted: list[RunLog] = []
        for log in logs:
            persisted.append(self.add(log))
        return persisted

    def exists(self, run_id: str, sequence: int) -> bool:
        """按 (run_id, sequence) 判断是否已存在（幂等去重）。"""
        orm = self._s.execute(
            select(RunLogORM.id).where(
                RunLogORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery(),
                RunLogORM.sequence == sequence,
            )
        ).scalar_one_or_none()
        return orm is not None

    def existing_sequences(self, run_id: str, sequences: list[int]) -> set[int]:
        """批量查询已存在的 sequence 集合，避免逐条 exists N+1。"""
        if not sequences:
            return set()
        rows = (
            self._s.execute(
                select(RunLogORM.sequence).where(
                    RunLogORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery(),
                    RunLogORM.sequence.in_(sequences),
                )
            )
            .scalars()
            .all()
        )
        return set(rows)

    def existing_attempt_sequences(self, run_id: str, attempt_id: str, sequences: list[int]) -> set[int]:
        """按 (run_id, attempt_id, sequence) 查询 V2 日志幂等键。"""
        if not sequences:
            return set()
        rows = (
            self._s.execute(
                select(RunLogORM.sequence).where(
                    RunLogORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery(),
                    RunLogORM.attempt_id == attempt_id,
                    RunLogORM.sequence.in_(sequences),
                )
            )
            .scalars()
            .all()
        )
        return set(rows)

    def list_by_run(self, run_id: str, *, after_sequence: int = 0) -> list[RunLog]:
        stmt = (
            select(RunLogORM)
            .options(joinedload(RunLogORM.run), joinedload(RunLogORM.shard))
            .where(RunLogORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery())
            .order_by(RunLogORM.sequence)
        )
        if after_sequence:
            stmt = stmt.where(RunLogORM.sequence > after_sequence)
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def get_max_sequence(self, run_id: str) -> int:
        """返回 Run 已落库的最大日志 sequence（无日志返回 0）。"""
        stmt = (
            select(RunLogORM.sequence)
            .where(RunLogORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery())
            .order_by(RunLogORM.sequence.desc())
            .limit(1)
        )
        return self._s.execute(stmt).scalar_one_or_none() or 0
