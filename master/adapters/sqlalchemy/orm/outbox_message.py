"""ORM：事务性 outbox（outbox_messages 表，P3.5）。

业务状态与待发送消息同一事务提交（§5.1）；worker 按 (status, next_attempt_at)
取到期消息发送，成功后标记 succeeded。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from master.domain.enums import OutboxStatus

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class OutboxMessage(Base, TimestampMixin):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index("ix_outbox_messages_status_attempt", "status", "next_attempt_at"),
        CheckConstraint(
            "status IN ('pending','sending','succeeded','retrying','exhausted','cancelled')",
            name="ck_outbox_messages_status",
        ),
        CheckConstraint("qos IN (0, 1, 2)", name="ck_outbox_messages_qos"),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 outbox_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:outbox_id Outbox 消息业务标识（ULID），全局唯一
    outbox_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:aggregate_type 所属聚合类型（如 task_run / node）
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:aggregate_id 所属聚合业务标识
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:topic 目标 MQTT 主题（派发/命令下发）
    topic: Mapped[str] = mapped_column(String(256), nullable=False)
    # sym:payload 消息载荷 JSON
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:qos MQTT QoS（0/1/2，CHECK 约束）
    qos: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # sym:status 投递状态（pending/sending/succeeded/retrying/exhausted/cancelled）
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OutboxStatus.PENDING.value
    )
    # sym:attempts 已尝试发送次数（重试计数）
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # sym:next_attempt_at 下次发送时间（失败退避；(status, next_attempt_at) 索引）
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime, nullable=True
    )
    # sym:sent_at 最近一次发送时间
    sent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
