"""Run 产物应用服务（P6.6，§6.2/§18.8）。

Agent 执行结束后上传报告/日志归档/数据文件；本服务负责：
- ``register_artifact``：写 ``run_artifacts`` 引用（校验 run 归属）；
- ``list_by_run``：项目范围产物列表；
- ``get_by_artifact_id``：项目范围产物详情（供下载）。

文件内容写入经 ``ArtifactStorageService``（Storage 端口），本服务只
负责引用与哈希登记。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Callable

from master.application.errors import RunNotFoundError
from master.application.services.artifact_storage_service import (
    ArtifactStorageService,
)
from master.domain.enums import ArtifactKind
from master.domain.models import RunArtifact
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)


class ArtifactService:
    """产物登记与查询。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        storage: ArtifactStorageService,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage

    def register_artifact(
        self,
        *,
        run_id: str,
        project_id: str,
        node_id: str,
        kind: str,
        filename: str,
        data: bytes,
        shard_id: str | None = None,
    ) -> RunArtifact:
        """上传并登记一个产物（写文件 + 写引用，同一业务操作）。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(run_id, project_id)
            if run is None:
                raise RunNotFoundError(f"Run 不存在或不属于当前项目: {run_id}")

            file_ref = self._storage.store(run_id, filename, data)
            digest = hashlib.sha256(data).hexdigest()
            artifact = RunArtifact(
                artifact_id=f"A-{uuid.uuid4().hex.upper()}",
                run_id=run_id,
                shard_id=shard_id,
                node_id=node_id,
                kind=ArtifactKind(kind),
                file_ref=file_ref,
                size=len(data),
                sha256=digest,
                uploaded_at=utcnow(),
            )
            created = uow.run_artifacts.add(artifact)
            logger.info(
                "产物登记成功: artifact=%s run=%s kind=%s size=%d",
                created.artifact_id,
                run_id,
                kind,
                len(data),
            )
            return created

    def list_by_run(self, run_id: str, project_id: str) -> list[RunArtifact]:
        """项目范围产物列表。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(run_id, project_id)
            if run is None:
                raise RunNotFoundError(f"Run 不存在或不属于当前项目: {run_id}")
            return uow.run_artifacts.list_by_run(run_id)

    def get_by_artifact_id(
        self, artifact_id: str, project_id: str
    ) -> RunArtifact | None:
        """项目范围产物详情。"""
        with self._uow_factory() as uow:
            artifact = uow.run_artifacts.get_by_artifact_id(artifact_id)
            if artifact is None:
                return None
            run = uow.task_runs.get_by_run_id(artifact.run_id, project_id)
            if run is None:
                return None
            return artifact

    def open(self, artifact: RunArtifact):
        """打开产物读取流（下载用）。"""
        return self._storage.open(artifact.file_ref)
