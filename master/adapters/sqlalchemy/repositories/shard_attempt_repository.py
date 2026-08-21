"""SQLAlchemy Shard 派发尝试仓储实现（P3.4，shard_attempts 表，D-20）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import RunShard as RunShardORM
from master.adapters.sqlalchemy.orm import ShardAttempt as ShardAttemptORM
from master.domain.enums import ShardAttemptStatus
from master.domain.models import ShardAttempt
from master.domain.repositories import ShardAttemptRepository


def _to_domain(orm: ShardAttemptORM) -> ShardAttempt:
    return ShardAttempt(
        id=orm.id,
        attempt_id=orm.attempt_id,
        shard_id=orm.shard.shard_id if orm.shard is not None else "",
        attempt_no=orm.attempt_no,
        node_id=orm.node_id,
        device_ids=list(orm.device_ids or []),
        status=ShardAttemptStatus(orm.status),
        error_code=orm.error_code,
        error_message=orm.error_message,
        started_at=orm.started_at,
        finished_at=orm.finished_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class ShardAttemptRepositoryImpl(ShardAttemptRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, attempt: ShardAttempt) -> ShardAttempt:
        shard_pk = self._s.execute(
            select(RunShardORM.id).where(RunShardORM.shard_id == attempt.shard_id)
        ).scalar_one_or_none()
        if shard_pk is None:
            raise ValueError(f"Shard 不存在: {attempt.shard_id}")
        orm = ShardAttemptORM(
            attempt_id=attempt.attempt_id,
            shard_pk=shard_pk,
            attempt_no=attempt.attempt_no,
            node_id=attempt.node_id,
            device_ids=attempt.device_ids,
            status=attempt.status.value,
            error_code=attempt.error_code,
            error_message=attempt.error_message,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_shard_attempt(self, shard_id: str, attempt_no: int) -> ShardAttempt | None:
        orm = (
            self._s.execute(
                select(ShardAttemptORM)
                .options(joinedload(ShardAttemptORM.shard))
                .where(
                    ShardAttemptORM.shard_pk
                    == select(RunShardORM.id).where(RunShardORM.shard_id == shard_id).scalar_subquery(),
                    ShardAttemptORM.attempt_no == attempt_no,
                )
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def get_by_attempt_id(self, attempt_id: str) -> ShardAttempt | None:
        """按 attempt 业务标识查询（dispatch_id == attempt_id，§8.4）。"""
        orm = (
            self._s.execute(
                select(ShardAttemptORM)
                .options(joinedload(ShardAttemptORM.shard))
                .where(ShardAttemptORM.attempt_id == attempt_id)
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def list_by_shard(self, shard_id: str) -> list[ShardAttempt]:
        stmt = (
            select(ShardAttemptORM)
            .options(joinedload(ShardAttemptORM.shard))
            .where(
                ShardAttemptORM.shard_pk
                == select(RunShardORM.id).where(RunShardORM.shard_id == shard_id).scalar_subquery()
            )
            .order_by(ShardAttemptORM.attempt_no)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def list_by_run(self, run_id: str) -> list[ShardAttempt]:
        """按 run_id 一次查回所有 attempt，避免 N+1 查询。"""
        from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM

        stmt = (
            select(ShardAttemptORM)
            .options(joinedload(ShardAttemptORM.shard))
            .join(ShardAttemptORM.shard)
            .where(RunShardORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery())
            .order_by(ShardAttemptORM.shard_pk, ShardAttemptORM.attempt_no)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def update(self, attempt: ShardAttempt) -> ShardAttempt:
        orm = self._s.get(ShardAttemptORM, attempt.id)
        if orm is None:
            raise ValueError(f"Attempt 不存在: id={attempt.id}")
        orm.node_id = attempt.node_id
        orm.device_ids = attempt.device_ids
        orm.status = attempt.status.value
        orm.error_code = attempt.error_code
        orm.error_message = attempt.error_message
        orm.started_at = attempt.started_at
        orm.finished_at = attempt.finished_at
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def list_active_by_node(self, node_id: str) -> list[ShardAttempt]:
        """查询指定节点上所有活跃（非终态）的 Attempt。"""
        stmt = (
            select(ShardAttemptORM)
            .options(joinedload(ShardAttemptORM.shard))
            .where(
                ShardAttemptORM.node_id == node_id,
                ShardAttemptORM.status.in_(
                    [
                        ShardAttemptStatus.DISPATCHED.value,
                        ShardAttemptStatus.ACKED.value,
                        ShardAttemptStatus.RUNNING.value,
                    ]
                ),
            )
            .order_by(ShardAttemptORM.id)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]
