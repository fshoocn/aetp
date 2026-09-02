"""ORM：结束产物引用（run_artifacts 表，P3.4）。

文件存 data/artifacts/{run_id}/，DB 只存引用与 sha256（§6.2）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from master.domain.enums import ArtifactKind

from .base import Base, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .run_shard import RunShard
    from .task_run import TaskRun


class RunArtifact(Base, TimestampMixin):
    __tablename__ = "run_artifacts"
    __table_args__ = (
        Index("ix_run_artifacts_run_kind", "run_pk", "kind"),
        CheckConstraint(
            "kind IN ('report','log_archive','data')",
            name="ck_run_artifacts_kind",
        ),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 artifact_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:artifact_id 产物业务标识（ULID），全局唯一
    artifact_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:run_pk 所属 Run 代理主键（Run 删除时级联清理）
    run_pk: Mapped[int] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    # sym:shard_pk 所属 Shard 代理主键（Run 级产物为空）
    shard_pk: Mapped[int | None] = mapped_column(ForeignKey("run_shards.id", ondelete="CASCADE"), nullable=True)
    # sym:node_id 上传节点业务 ID
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sym:attempt_id V2 Attempt 来源
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sym:kind 产物类型（report/log_archive/data）
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=ArtifactKind.REPORT.value)
    # sym:file_ref 文件引用路径（data/artifacts/{run_id}/...）
    file_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    # sym:filename 原始文件名
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # sym:content_type MIME 类型
    content_type: Mapped[str] = mapped_column(String(255), nullable=False, default="application/octet-stream")
    # sym:size 文件字节数
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # sym:sha256 内容哈希（下载校验）
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:derived_from 来源 Artifact
    derived_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sym:uploaded_at 上传时间（UTC）
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=datetime.utcnow)

    # sym:run 所属 Run ORM 关系
    run: Mapped[TaskRun] = relationship()
    # sym:shard 所属 Shard ORM 关系（Run 级产物为空）
    shard: Mapped[RunShard | None] = relationship()
