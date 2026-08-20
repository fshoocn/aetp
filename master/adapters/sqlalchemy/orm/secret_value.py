"""ORM：加密密钥记录（secret_values 表，§12.2）。"""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class SecretValue(Base, TimestampMixin):
    __tablename__ = "secret_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secret_ref: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    cipher_text: Mapped[str] = mapped_column(Text, nullable=False)
