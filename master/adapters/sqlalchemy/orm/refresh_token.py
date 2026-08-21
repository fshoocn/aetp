"""ORM：刷新令牌会话记录。

只存 SHA-256 哈希，不存原始令牌；登出/改密/禁用账户时置 revoked_at。
rotation 链（replaced_by_hash）用于将来检测旧令牌重放攻击。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, UTCDateTime


class RefreshToken(Base, TimestampMixin):
    __tablename__ = "refresh_tokens"

    # sym:id 代理主键（自增 int）
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # sym:user_pk 所属用户代理主键；会话令牌属临时数据，用户删除时级联清理
    user_pk: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # sym:token_hash 刷新令牌 SHA-256 哈希（唯一），不存原始令牌，数据库泄露不暴露凭据
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # sym:expires_at 过期时间；过期后拒绝刷新（P2.10）
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # sym:revoked_at 撤销时间（登出/改密/禁用账户），非空即失效
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # sym:replaced_by_hash 轮换链：本令牌被哪个新令牌哈希替代，用于旧令牌重放检测
    replaced_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
