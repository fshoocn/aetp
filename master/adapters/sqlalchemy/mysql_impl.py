"""MySQL / MariaDB 数据库实现（需要 pymysql 驱动）。"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .base_impl import BaseDatabase
from .database_interface import DatabaseConfig


class MySQLDatabase(BaseDatabase):
    DEFAULT_DRIVER = "pymysql"
    SUPPORTED_SCHEMES = ("mysql", "mariadb")

    def _make_engine(self, url: str, config: DatabaseConfig) -> Engine:
        _ensure_driver_installed("pymysql", "MySQL")
        connect_args = dict(config.connect_args)
        # 常用默认：utf8mb4 字符集
        connect_args.setdefault("charset", "utf8mb4")
        return create_engine(url, **config.engine_kwargs, connect_args=connect_args)


def _ensure_driver_installed(driver: str, db_label: str) -> None:
    try:
        __import__(driver)
    except ImportError as exc:
        raise RuntimeError(f"使用 {db_label} 需要安装驱动 {driver}: pip install {driver}") from exc
