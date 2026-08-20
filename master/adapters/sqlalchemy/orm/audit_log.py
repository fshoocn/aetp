"""ORM：审计日志（audit_logs 表，P3.5）。

append-only 记录表：不设 FK（actor/project 删除不影响审计留存），
敏感操作（账户审批、成员/角色变更、CI 密钥变更等 §7.6）必须写入。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, UTCDateTime


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_project_occurred", "project_id", "occurred_at"),
        Index("ix_audit_logs_actor_occurred", "actor_id", "occurred_at"),
    )

    # sym:id 代理主键（自增 int），仅供内部引用；对外业务标识用 audit_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:audit_id 审计业务标识（ULID），全局唯一
    audit_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:project_id 所属项目业务标识（平台级操作可为空，无 FK 保留审计）
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sym:actor_id 操作者用户代理主键（系统操作可为空，无 FK 防止删除受阻）
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # sym:action 动作名（如 member.add / role.change / integration.key_rotate）
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:resource_type 被操作资源类型（project/member/task/...）
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:resource_id 被操作资源业务标识
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # sym:request_id 关联 HTTP 请求追踪 ID（可空）
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # sym:detail 审计详情 JSON（变更前后值等）
    detail: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # sym:occurred_at 操作发生时间（UTC）
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=datetime.utcnow
    )
