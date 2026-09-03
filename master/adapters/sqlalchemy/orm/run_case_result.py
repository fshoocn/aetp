"""ORM：case 级执行结果（run_case_results 表，P3.4）。

(run_pk, shard_pk, case_key, attempt_no) 唯一；按 attempt 全量保留（D-20）。
pytest 类插件可运行中实时上报；CANoe 类仅结束后由 parse_results 产出（D-19）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from master.domain.enums import CaseStatus

from .base import Base, JSONType, TimestampMixin

if TYPE_CHECKING:
    from .run_shard import RunShard
    from .task_run import TaskRun


class RunCaseResult(Base, TimestampMixin):
    __tablename__ = "run_case_results"
    __table_args__ = (
        UniqueConstraint(
            "run_pk",
            "shard_pk",
            "case_key",
            "attempt_no",
            name="uq_run_case_results_run_shard_case_attempt",
        ),
        Index("ix_run_case_results_run", "run_pk"),
        CheckConstraint(
            "status IN ('pending','running','passed','failed','skipped','error')",
            name="ck_run_case_results_status",
        ),
    )

    # sym:id 代理主键（自增 int）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:run_pk 所属 Run 代理主键（Run 删除时级联清理）
    run_pk: Mapped[int] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    # sym:shard_pk 所属 Shard 代理主键
    shard_pk: Mapped[int] = mapped_column(ForeignKey("run_shards.id", ondelete="CASCADE"), nullable=False, index=True)
    # sym:case_key 用例稳定标识（stable_key）
    case_key: Mapped[str] = mapped_column(String(256), nullable=False)
    # sym:attempt_no 关联的派发尝试序号
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # sym:sequence case-status 序号
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # sym:status case 结果状态（passed/failed/skipped/error/...）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CaseStatus.PENDING.value)
    # sym:duration_ms 执行耗时（毫秒；仅成功统计 avg_duration_s 数据源，D-21）
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # sym:error_summary 失败/错误摘要
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # sym:detail 结构化详情 JSON（断言信息、堆栈等）
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    # sym:run 所属 Run ORM 关系
    run: Mapped[TaskRun] = relationship()
    # sym:shard 所属 Shard ORM 关系
    shard: Mapped[RunShard] = relationship()
