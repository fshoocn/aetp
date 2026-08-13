"""ORM：不可变领域事件（domain_events 表，P3.5）。

业务事务提交时一并写入；sequence 全局单调唯一（add 时分配 MAX+1），
保证事件顺序（SSE/Hook/通知按此消费，§5.1）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class DomainEvent(Base, TimestampMixin):
    __tablename__ = "domain_events"
    __table_args__ = (
        UniqueConstraint("sequence", name="uq_domain_events_sequence"),
        Index("ix_domain_events_project_sequence", "project_id", "sequence"),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 event_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:event_id 事件业务标识（ULID），全局唯一
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:sequence 全局单调序号（唯一，事件顺序依据；add 时分配 MAX+1）
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # sym:project_id 所属项目业务标识（平台级事件为空，无 FK 保留业务键）
    project_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # sym:event_type 事件类型（如 run.created / run.attempt_failed，§10 事件清单）
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:aggregate_id 关联聚合业务标识（run_id / task_id / node_id 等）
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:payload 事件载荷 JSON（不可变快照）
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:occurred_at 业务发生时间（UTC）
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=datetime.utcnow
    )
