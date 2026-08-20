"""SQLAlchemy ORM 基类与通用约定。

ORM 模型仅负责表结构映射，不含业务逻辑；业务逻辑在 domain/models 中。
模型定义即数据库表结构的唯一事实源（配合 Alembic 迁移）。

UTCDateTime：统一以 UTC 存储/读取 datetime。
JSONType：结构化 JSON 列（PostgreSQL 使用 JSONB，其余方言使用 JSON）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, MetaData, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 统一的命名约定：约束/索引名稳定，便于迁移与排错
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator):
    """统一 UTC datetime 类型。

    - 写入：任意带时区的 datetime 统一转为 UTC，并以 naive UTC 落库
      （SQLite/MySQL 不保存时区，PostgreSQL 亦可接受）；
    - 读取：统一补上 UTC 时区标记，保证应用层拿到的是 tz-aware UTC。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    """Python 侧默认时间（UTC，带时区）。"""
    return datetime.now(UTC)


# 结构化 JSON 列类型：PostgreSQL 用 JSONB 便于查询，其余用 JSON
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """统一 created_at / updated_at 字段（UTC）。"""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
