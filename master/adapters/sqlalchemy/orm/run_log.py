"""ORM：Run 执行日志（run_logs 表，§6.2）。

Run 级日志由 Agent 以 ``run.log``（RunLogBatch）上报，(run_pk, sequence)
唯一实现接收端幂等去重；与旧 ``task_logs``（task 级日志，关联 tasks）解耦，
任务日志归属于 Run 执行域。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .run_shard import RunShard
    from .task_run import TaskRun


class RunLog(Base, TimestampMixin):
    __tablename__ = "run_logs"
    __table_args__ = (
        UniqueConstraint("run_pk", "sequence", name="uq_run_logs_run_sequence"),
        Index("ix_run_logs_run_sequence", "run_pk", "sequence"),
        CheckConstraint(
            "level IN ('debug','info','warn','error')",
            name="ck_run_logs_level",
        ),
    )

    # sym:id 代理主键（自增 int）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:run_pk 所属 Run 代理主键（Run 删除时级联清理）
    run_pk: Mapped[int] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    # sym:shard_pk 所属 Shard 代理主键（Run 级日志可空）
    shard_pk: Mapped[int | None] = mapped_column(ForeignKey("run_shards.id", ondelete="CASCADE"), nullable=True)
    # sym:node_id 产生日志的 Agent 节点业务 ID（无 FK 保留业务键）
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # sym:sequence Run 内单调递增序号；(run_pk, sequence) 唯一
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # sym:level 日志等级（debug/info/warn/error）
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    # sym:message 日志正文
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # sym:detail 结构化详情 JSON
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # sym:occurred_at 产生时间（UTC）
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=datetime.utcnow)

    # sym:run 所属 Run ORM 关系
    run: Mapped[TaskRun] = relationship()
    # sym:shard 所属 Shard ORM 关系（Run 级日志为空）
    shard: Mapped[RunShard | None] = relationship()
