"""ORM：平台用户。"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from master.domain.enums import AccountStatus, PlatformRole

from .base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    account_status: Mapped[str] = mapped_column(String(16), nullable=False, default=AccountStatus.PENDING.value)
    platform_role: Mapped[str] = mapped_column(String(16), nullable=False, default=PlatformRole.USER.value)

    __table_args__ = (
        CheckConstraint(
            "account_status IN ('pending','active','disabled')",
            name="ck_users_account_status",
        ),
        CheckConstraint(
            "platform_role IN ('user','admin')",
            name="ck_users_platform_role",
        ),
    )
