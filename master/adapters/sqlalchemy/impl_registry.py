"""数据库实现注册表。

新增数据库支持时：在 impl/ 下新建实现类并注册到 REGISTRY 即可，
工厂层（db_factory.py）会自动按 scheme 分发。
"""

from __future__ import annotations

from typing import Type

from .base_impl import BaseDatabase
from .mysql_impl import MySQLDatabase
from .postgres_impl import PostgresDatabase
from .sqlite_impl import SQLiteDatabase

# scheme -> 实现类
REGISTRY: dict[str, Type[BaseDatabase]] = {
    "sqlite": SQLiteDatabase,
    "mysql": MySQLDatabase,
    "mariadb": MySQLDatabase,
    "postgresql": PostgresDatabase,
    "postgres": PostgresDatabase,
}

__all__ = [
    "REGISTRY",
    "BaseDatabase",
    "SQLiteDatabase",
    "MySQLDatabase",
    "PostgresDatabase",
]
