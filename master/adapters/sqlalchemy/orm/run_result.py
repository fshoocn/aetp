"""ORM：Run 级汇总投影（results 表，P3.4）。

由各 Shard/Attempt/case 结果投影计算（§5.4 规则 4）；run_pk 唯一，
一 Run 一行，不重复存 Shard 明细。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from master.domain.enums import RunStatus

from .base import Base, JSONType, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .project import Project
    from .task_run import TaskRun
    from .test_task import TestTask


class RunResult(Base, TimestampMixin):
    __tablename__ = "results"
    __table_args__ = (
        UniqueConstraint("run_pk", name="uq_results_run"),
        CheckConstraint(
            "status IN ('created','dispatched','acked','running','succeeded',"
            "'failed','cancelled','timed_out','lost')",
            name="ck_results_status",
        ),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 result_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:result_id 汇总投影业务标识（ULID），全局唯一
    result_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:run_pk 对应 Run 代理主键（唯一，一 Run 一行投影）
    run_pk: Mapped[int] = mapped_column(
        ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # sym:project_pk 所属项目代理主键
    project_pk: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # sym:task_pk 任务定义代理主键（任务删除后置空，Run 级汇总仍可展示）
    task_pk: Mapped[int | None] = mapped_column(
        ForeignKey("test_tasks.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # sym:node_id 最终执行节点业务 ID（多 Shard 场景可为空）
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sym:passed 是否全部通过（供列表快速展示）
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # sym:status Run 总体状态
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RunStatus.CREATED.value
    )
    # sym:metrics 汇总指标 JSON（总耗时、通过/失败数等）
    metrics: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # sym:data 汇总数据 JSON
    data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # sym:started_at 开始时间（UTC）
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # sym:finished_at 结束时间（UTC）
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # sym:run 对应 Run ORM 关系
    run: Mapped[TaskRun] = relationship()
    # sym:project 所属项目 ORM 关系
    project: Mapped[Project] = relationship()
    # sym:task 任务定义 ORM 关系
    task: Mapped[TestTask] = relationship()
