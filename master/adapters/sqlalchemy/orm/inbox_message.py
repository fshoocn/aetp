"""ORM：入站消息去重（inbox_messages 表，P3.5）。

(origin_id, message_id) 唯一实现幂等去重：MQTT 重复投递只处理一次（§5.4）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UTCDateTime


class InboxMessage(Base, TimestampMixin):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        UniqueConstraint(
            "origin_id", "message_id", name="uq_inbox_messages_origin_message"
        ),
    )

    # sym:id 代理主键（自增 int）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:origin_id 消息来源标识（如 MQTT client_id / node_id / 集成名）
    origin_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # sym:message_id 消息源侧唯一 ID（(origin_id, message_id) 唯一=幂等去重键）
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # sym:message_type 消息类型（命令/结果/心跳等）
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:payload_hash 载荷哈希（内容校验与重复检测）
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:received_at 接收时间（UTC）
    received_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=datetime.utcnow
    )
    # sym:processed_at 处理完成时间（非空=已处理）
    processed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
