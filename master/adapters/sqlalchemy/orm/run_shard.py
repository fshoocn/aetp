"""ORM：Run 内的 Shard（run_shards 表，P3.4）。

由插件 split_shards 在 Run 创建时分割产出；case_keys 为该 Shard 负责的
case 集合；物理设备由 Master 按脚本资源需求原子分配。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from master.domain.enums import ShardStatus

from .base import Base, JSONType, TimestampMixin

if TYPE_CHECKING:
    from .task_run import TaskRun


class RunShard(Base, TimestampMixin):
    __tablename__ = "run_shards"
    __table_args__ = (
        UniqueConstraint("run_pk", "shard_index", name="uq_run_shards_run_index"),
        Index("ix_run_shards_run_status", "run_pk", "status"),
        CheckConstraint(
            "status IN ('pending','dispatching','running','waiting_recovery',"
            "'succeeded','failed','cancelled','timed_out')",
            name="ck_run_shards_status",
        ),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 shard_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:shard_id Shard 业务标识（ULID），全局唯一
    shard_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:script_binding_id 任务脚本绑定
    script_binding_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # sym:run_pk 所属 Run 代理主键（Run 删除时级联清理）
    run_pk: Mapped[int] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    # sym:shard_index Run 内序号；(run_pk, shard_index) 唯一
    shard_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # sym:case_keys 该 Shard 负责的 case 集合（stable_key 列表）
    case_keys: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # sym:execution_params 该子任务专属执行参数 JSON（插件 split_shards 产出，
    #   Agent 插件 execute 使用；与共享 config 合并/覆盖）
    execution_params: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:estimated_duration_s 预估耗时（秒；null=未知）
    estimated_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # sym:status Shard 状态（pending/dispatching/running/...，§5.4）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ShardStatus.PENDING.value)
    # sym:final_node 最终执行节点业务 ID（多 attempt 后取最后有效者）
    final_node: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # sym:run 所属 Run ORM 关系
    run: Mapped[TaskRun] = relationship()
