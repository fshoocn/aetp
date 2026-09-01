"""ORM：Shard 派发尝试（shard_attempts 表，P3.4）。

(shard_pk, attempt_no) 唯一；换节点 failover 递增 attempt_no，
历史失败全量保留（D-20，不得覆盖）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from master.domain.enums import ShardAttemptStatus

from .base import Base, JSONType, TimestampMixin, UTCDateTime

if TYPE_CHECKING:
    from .run_shard import RunShard


class ShardAttempt(Base, TimestampMixin):
    __tablename__ = "shard_attempts"
    __table_args__ = (
        UniqueConstraint("shard_pk", "attempt_no", name="uq_shard_attempts_shard_attempt"),
        CheckConstraint(
            "status IN ('created','dispatched','acked','running','unknown','succeeded',"
            "'failed','cancelled','timed_out','lost')",
            name="ck_shard_attempts_status",
        ),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 attempt_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:attempt_id Attempt 业务标识（ULID），全局唯一
    attempt_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:shard_pk 所属 Shard 代理主键（Shard 删除时级联清理）
    shard_pk: Mapped[int] = mapped_column(ForeignKey("run_shards.id", ondelete="CASCADE"), nullable=False, index=True)
    # sym:attempt_no 尝试序号（自 1 递增）；(shard_pk, attempt_no) 唯一
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # sym:node_id 执行节点业务 ID（failover 换节点时变化，无 FK 保留业务键）
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:device_ids 本次 Attempt 占用的全部设备业务 ID；历史 Attempt 可为空
    device_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # sym:status 尝试状态（created/dispatched/acked/running/...）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ShardAttemptStatus.CREATED.value)
    # sym:error_code 领域错误码（如 NODE_CAPABILITY_MISMATCH）
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sym:error_message 失败描述（历史失败信息全量保留，D-20）
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # sym:started_at 开始执行时间
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # sym:finished_at 结束时间
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # sym:shard 所属 Shard ORM 关系
    shard: Mapped[RunShard] = relationship()
