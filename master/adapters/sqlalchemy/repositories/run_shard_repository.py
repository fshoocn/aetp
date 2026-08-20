"""SQLAlchemy Shard 仓储实现（P3.4，run_shards 表）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import RunShard as RunShardORM
from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM
from master.domain.enums import ShardStatus
from master.domain.models import RunShard
from master.domain.repositories import RunShardRepository


def _to_domain(orm: RunShardORM) -> RunShard:
    return RunShard(
        id=orm.id,
        shard_id=orm.shard_id,
        run_id=orm.run.run_id if orm.run is not None else "",
        shard_index=orm.shard_index,
        case_keys=list(orm.case_keys or []),
        execution_params=dict(orm.execution_params or {}),
        estimated_duration_s=orm.estimated_duration_s,
        status=ShardStatus(orm.status),
        final_node=orm.final_node,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class RunShardRepositoryImpl(RunShardRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, shard: RunShard) -> RunShard:
        return self.add_many([shard])[0]

    def add_many(self, shards: list[RunShard]) -> list[RunShard]:
        run_pk = None
        first_run_id = shards[0].run_id if shards else ""
        for shard in shards:
            if run_pk is None or shard.run_id != first_run_id:
                run_pk = self._s.execute(
                    select(TaskRunORM.id).where(TaskRunORM.run_id == shard.run_id)
                ).scalar_one_or_none()
            if run_pk is None:
                raise ValueError(f"Run 不存在: {shard.run_id}")
            orm = RunShardORM(
                shard_id=shard.shard_id,
                run_pk=run_pk,
                shard_index=shard.shard_index,
                case_keys=shard.case_keys,
                execution_params=shard.execution_params,
                estimated_duration_s=shard.estimated_duration_s,
                status=shard.status.value,
                final_node=shard.final_node,
            )
            self._s.add(orm)
        self._s.flush()
        return [
            self.get_by_shard_id(s.shard_id) or s for s in shards
        ]

    def get_by_shard_id(self, shard_id: str) -> RunShard | None:
        orm = self._s.execute(
            select(RunShardORM)
            .options(joinedload(RunShardORM.run))
            .where(RunShardORM.shard_id == shard_id)
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list_by_run(self, run_id: str) -> list[RunShard]:
        stmt = (
            select(RunShardORM)
            .options(joinedload(RunShardORM.run))
            .where(
                RunShardORM.run_pk
                == select(TaskRunORM.id)
                .where(TaskRunORM.run_id == run_id)
                .scalar_subquery()
            )
            .order_by(RunShardORM.shard_index, RunShardORM.id)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def update(self, shard: RunShard) -> RunShard:
        orm = self._s.get(RunShardORM, shard.id)
        if orm is None:
            raise ValueError(f"Shard 不存在: id={shard.id}")
        orm.case_keys = shard.case_keys
        orm.execution_params = shard.execution_params
        orm.estimated_duration_s = shard.estimated_duration_s
        orm.status = shard.status.value
        orm.final_node = shard.final_node
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def list_by_status(self, *statuses: ShardStatus) -> list[RunShard]:
        stmt = (
            select(RunShardORM)
            .options(joinedload(RunShardORM.run))
            .where(RunShardORM.status.in_([s.value for s in statuses]))
            .order_by(RunShardORM.id)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]
