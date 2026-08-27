"""SQLAlchemy 结束产物仓储实现（P3.4，run_artifacts 表）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import RunArtifact as RunArtifactORM
from master.adapters.sqlalchemy.orm import RunShard as RunShardORM
from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM
from master.domain.enums import ArtifactKind
from master.domain.models import RunArtifact
from master.domain.repositories import RunArtifactRepository


def _to_domain(orm: RunArtifactORM) -> RunArtifact:
    return RunArtifact(
        id=orm.id,
        artifact_id=orm.artifact_id,
        run_id=orm.run.run_id if orm.run is not None else "",
        shard_id=orm.shard.shard_id if orm.shard is not None else None,
        node_id=orm.node_id,
        kind=ArtifactKind(orm.kind),
        file_ref=orm.file_ref,
        size=orm.size,
        sha256=orm.sha256,
        uploaded_at=orm.uploaded_at,
        created_at=orm.created_at,
    )


class RunArtifactRepositoryImpl(RunArtifactRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, artifact: RunArtifact) -> RunArtifact:
        run_pk = self._s.execute(select(TaskRunORM.id).where(TaskRunORM.run_id == artifact.run_id)).scalar_one_or_none()
        if run_pk is None:
            raise ValueError(f"Run 不存在: {artifact.run_id}")
        shard_pk = None
        if artifact.shard_id is not None:
            shard_pk = self._s.execute(
                select(RunShardORM.id).where(RunShardORM.shard_id == artifact.shard_id)
            ).scalar_one_or_none()
            if shard_pk is None:
                raise ValueError(f"Shard 不存在: {artifact.shard_id}")
        orm = RunArtifactORM(
            artifact_id=artifact.artifact_id,
            run_pk=run_pk,
            shard_pk=shard_pk,
            node_id=artifact.node_id,
            kind=artifact.kind.value,
            file_ref=artifact.file_ref,
            size=artifact.size,
            sha256=artifact.sha256,
            uploaded_at=artifact.uploaded_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_artifact_id(self, artifact_id: str) -> RunArtifact | None:
        orm = (
            self._s.execute(
                select(RunArtifactORM)
                .options(
                    joinedload(RunArtifactORM.run),
                    joinedload(RunArtifactORM.shard),
                )
                .where(RunArtifactORM.artifact_id == artifact_id)
            )
            .scalars()
            .one_or_none()
        )
        return _to_domain(orm) if orm is not None else None

    def list_by_run(self, run_id: str) -> list[RunArtifact]:
        stmt = (
            select(RunArtifactORM)
            .options(
                joinedload(RunArtifactORM.run),
                joinedload(RunArtifactORM.shard),
            )
            .where(RunArtifactORM.run_pk == select(TaskRunORM.id).where(TaskRunORM.run_id == run_id).scalar_subquery())
            .order_by(RunArtifactORM.uploaded_at, RunArtifactORM.id)
        )
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def list_all_file_refs(self) -> list[str]:
        refs = self._s.execute(select(RunArtifactORM.file_ref)).scalars().all()
        return [r for r in refs if r]
