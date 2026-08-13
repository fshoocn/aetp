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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 会话令牌属临时数据，随用户删除级联清理（用户表不提供删除能力时无害）
    user_pk: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, nullable=True
    )
    replaced_by_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
