"""PostgreSQL 数据库实现（需要 psycopg2 驱动）。"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .base_impl import BaseDatabase
from .database_interface import DatabaseConfig


class PostgresDatabase(BaseDatabase):
    DEFAULT_DRIVER = "psycopg2"
    SUPPORTED_SCHEMES = ("postgresql", "postgres")

    def _make_engine(self, url: str, config: DatabaseConfig) -> Engine:
        try:
            import psycopg2  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "使用 PostgreSQL 需要安装驱动 psycopg2: "
                "pip install psycopg2-binary"
            ) from exc
        return create_engine(url, **config.engine_kwargs)
