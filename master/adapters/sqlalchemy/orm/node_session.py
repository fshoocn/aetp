"""ORM：节点 MQTT 会话（P4.4，§6.2 node_sessions）。

每次 Agent 进程启动生成一个新 session_id；新会话注册时旧会话关闭
（SESSION_REPLACED），旧 session 的后续消息被拒绝（P4.4 验收）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UTCDateTime


class NodeSession(Base, TimestampMixin):
    __tablename__ = "node_sessions"
    __table_args__ = (
        UniqueConstraint(
            "node_pk", "session_id", name="uq_node_sessions_node_session"
        ),
        Index("ix_node_sessions_node_current", "node_pk", "disconnected_at"),
    )

    # sym:id 代理主键（自增 int）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:node_pk 所属节点代理主键；节点删除时级联清理会话
    node_pk: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    # sym:node_id 节点业务标识（冗余，便于查询展示）
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:session_id Agent 进程启动生成的会话 ID（envelope.sender.session_id）
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:client_id MQTT client_id（诊断用）
    client_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # sym:connected_at 会话建立时间（UTC）
    connected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # sym:disconnected_at 会话关闭时间（非空=已关闭）
    disconnected_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    # sym:disconnect_reason 断开原因（DisconnectReason）
    disconnect_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
