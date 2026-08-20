"""SQLite 数据库实现（内置驱动，无需额外依赖）。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from .base_impl import BaseDatabase
from .database_interface import DatabaseConfig


class SQLiteDatabase(BaseDatabase):
    DEFAULT_DRIVER = "pysqlite"
    SUPPORTED_SCHEMES = ("sqlite",)

    def _make_engine(self, url: str, config: DatabaseConfig) -> Engine:
        url = self._resolve_path(url)

        connect_args = dict(config.connect_args)
        connect_args.setdefault("check_same_thread", False)
        engine = create_engine(url, **config.engine_kwargs, connect_args=connect_args)
        event.listen(engine, "connect", _sqlite_pragma_fk)
        return engine

    @staticmethod
    def _resolve_path(url: str) -> str:
        """把 SQLite 相对路径解析为基于运行目录的绝对连接串。"""
        from master.config import resolve_sqlite_url

        return resolve_sqlite_url(url)


def _sqlite_pragma_fk(dbapi_connection: Any, _: Any) -> None:
    """SQLite 连接建立时启用外键约束。"""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception as exc:  # noqa: BLE001 - 非关键路径，外键 pragma 失败不阻断连接
        logging.getLogger(__name__).debug("PRAGMA foreign_keys 设置失败: %s", exc)
