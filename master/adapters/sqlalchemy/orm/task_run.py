"""ORM：Run 执行（task_runs 表，P3.4）。

Run 是任务定义的一次执行快照：script_ref / case_selection / split_policy
均在创建时固化（§7.5）；trigger_type 记录触发来源（§18.7）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from master.domain.enums import RunStatus, TriggerType

from .base import Base, JSONType, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .project import Project
    from .user import User


class TaskRun(Base, TimestampMixin):
    __tablename__ = "task_runs"
    __table_args__ = (
        Index("ix_task_runs_project_status_created", "project_pk", "status", "created_at"),
        Index("ix_task_runs_project_trigger_created", "project_pk", "trigger_type", "created_at"),
        Index("ix_task_runs_task_created", "task_id", "created_at"),
        CheckConstraint(
            "trigger_type IN ('manual_web','api','schedule','ci_webhook','retry','recovery')",
            name="ck_task_runs_trigger_type",
        ),
        CheckConstraint(
            "status IN ('created','dispatched','acked','running','succeeded','failed','cancelled','timed_out','lost')",
            name="ck_task_runs_status",
        ),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 run_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:run_id Run 业务标识（ULID），全局唯一，对外暴露
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:task_id 任务业务标识
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # sym:project_pk 所属项目代理主键（Run 的 project 必须与任务定义一致）
    project_pk: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    # sym:task_revision 任务 revision
    task_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # sym:script_ref 脚本引用快照 {script_id, version, sha256}（§7.5）
    script_ref: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:case_selection 本次 Run 生效的 case 集合（默认集合或 case_filter 覆盖，D-15）
    case_selection: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # sym:split_policy 本次 Run 的分割策略快照（§18.6）
    split_policy: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:task_snapshot Run 的不可变完整 Snapshot
    task_snapshot: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False)
    # sym:trigger_type 触发来源（manual_web/api/schedule/ci_webhook/retry/recovery）
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False, default=TriggerType.MANUAL_WEB.value)
    # sym:triggered_by_user_pk 触发用户代理主键（系统触发时为空）
    triggered_by_user_pk: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    # sym:integration_id 触发来源 CI 集成标识（非 CI 触发为空；集成表后续阶段建）
    integration_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sym:trigger_context 触发上下文 JSON（retry 引用原 run_id、webhook 事件等）
    trigger_context: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # sym:status Run 总体状态（created/dispatched/acked/running/...，§6.4）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RunStatus.CREATED.value)
    # sym:started_at 首次进入 running 的时间
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # sym:finished_at 进入终态的时间
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # sym:log_complete 日志围栏：Agent 发布 run.log-complete 后置位（P6.6）
    log_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # sym:last_log_sequence 围栏时记录的末条日志 sequence
    last_log_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # sym:project 所属项目 ORM 关系
    project: Mapped[Project] = relationship()
    # sym:triggered_by_user 触发用户 ORM 关系
    triggered_by_user: Mapped[User | None] = relationship()
