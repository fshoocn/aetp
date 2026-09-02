"""数据库抽象接口层。

定义统一的 DatabaseInterface 抽象接口与 DatabaseConfig 连接配置。
实现层（impl/）针对不同数据库（sqlite / mysql / postgresql ...）提供具体实现，
工厂层（db_factory.py）根据连接信息自动匹配驱动并返回对应实现。

用法:
    from master.adapters.sqlalchemy.database_factory import create_database

    db = create_database("mysql://user:pass@host:3306/aetp")
    db.connect()          # 自动建表 + 结构同步
    with db.session_scope() as s:
        ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class DatabaseConfig:
    """数据库连接信息。

    两种用法：
    1. 直接给 url（推荐）:  DatabaseConfig(url="sqlite:///data/aetp.db")
    2. 拆分字段自动拼 url:   DatabaseConfig(db_type="mysql", host="...", ...)
    """

    # 完整连接串（优先使用）
    url: str = ""
    # 拆分连接信息（url 为空时使用）
    db_type: str = ""  # sqlite / mysql / postgresql ...
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    database: str = ""
    # 透传给 SQLAlchemy
    connect_args: dict[str, Any] = field(default_factory=dict)
    engine_kwargs: dict[str, Any] = field(default_factory=dict)
    v2_only: bool = False

    def build_url(self) -> str:
        """返回最终连接串；url 为空时由拆分字段拼接。"""
        if self.url:
            return self.url
        t = self.db_type.lower()
        if t == "sqlite":
            return f"sqlite:///{self.database or 'data/aetp.db'}"
        if t in ("mysql", "mariadb"):
            return f"mysql+pymysql://{self.username}:{self.password}@{self.host}:{self.port or 3306}/{self.database}"
        if t in ("postgresql", "postgres"):
            return (
                f"postgresql+psycopg2://{self.username}:{self.password}@{self.host}:{self.port or 5432}/{self.database}"
            )
        raise ValueError(f"不支持的数据库类型: {self.db_type!r}（支持: sqlite / mysql / mariadb / postgresql）")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> DatabaseConfig:
        """从字典构造配置，自动忽略未知字段。"""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


class DatabaseInterface(ABC):
    """数据库统一接口：所有实现都必须提供的能力。"""

    config: DatabaseConfig
    engine: Engine

    @property
    @abstractmethod
    def db_type(self) -> str:
        """数据库类型名（sqlite / mysql / postgresql）。"""

    @abstractmethod
    def connect(self) -> list[str]:
        """建立连接并执行 Alembic 迁移。

        返回执行的同步动作列表。
        """

    @abstractmethod
    def sync_schema(self) -> list[str]:
        """显式执行 Alembic 迁移，返回迁移动作列表。"""

    @abstractmethod
    def session(self) -> Session:
        """获取一个独立 Session（调用方负责 close）。"""

    @contextmanager
    @abstractmethod
    def session_scope(self) -> Generator[Session, None, None]:
        """事务性 Session 上下文：正常提交，异常回滚。"""

    @abstractmethod
    def close(self) -> None:
        """释放连接池等资源。"""
