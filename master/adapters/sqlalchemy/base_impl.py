"""SQLAlchemy 通用实现基类。

封装所有数据库共用的逻辑：
- 根据连接串创建 engine（驱动补全由子类负责）；
- 根据 ORM metadata 初始化当前 schema（线程安全，见 _SCHEMA_LOCK）；
- Session 工厂与会话上下文。

各具体数据库实现（sqlite / mysql / postgresql ...）继承本类，
只需提供驱动名与各自的 engine 定制。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .database_interface import DatabaseConfig, DatabaseInterface

logger = logging.getLogger(__name__)

# schema 初始化互斥锁：防止同一进程内重复创建基线
_SCHEMA_LOCK = threading.Lock()


class BaseDatabase(DatabaseInterface):
    """基于 SQLAlchemy 的通用数据库实现。"""

    # 子类必须覆盖：默认驱动名（如 pysqlite / pymysql）
    DEFAULT_DRIVER: str = ""
    # 子类覆盖：支持的 scheme 集合
    SUPPORTED_SCHEMES: tuple[str, ...] = ()

    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config
        url = self._ensure_driver(config.build_url())
        self.engine = self._make_engine(url, config)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @property
    def db_type(self) -> str:
        return self.SUPPORTED_SCHEMES[0] if self.SUPPORTED_SCHEMES else "unknown"

    # ---- 子类可覆盖 ----

    def _ensure_driver(self, url: str) -> str:
        """补全驱动：mysql://  -> mysql+pymysql://"""
        scheme, _, rest = url.partition("://")
        if not rest:
            raise ValueError(f"无法解析数据库连接串: {url!r}")
        if "+" in scheme:
            return url
        return f"{scheme}+{self.DEFAULT_DRIVER}://{rest}"

    def _make_engine(self, url: str, config: DatabaseConfig) -> Engine:
        """创建 Engine；子类可覆盖以做方言定制。"""
        from sqlalchemy import create_engine

        return create_engine(url, **config.engine_kwargs)

    # ---- schema 初始化 ----

    def connect(self) -> list[str]:
        """建立连接并初始化当前 ORM schema。"""
        return self._initialize_schema()

    def _initialize_schema(self) -> list[str]:
        from master.adapters.sqlalchemy.schema import METADATA, SCHEMA_VERSION

        with _SCHEMA_LOCK, self.engine.begin() as connection:
            METADATA.create_all(connection)
            connection.exec_driver_sql(
                "CREATE TABLE IF NOT EXISTS aetp_schema_version "
                "(version VARCHAR(32) NOT NULL)"
            )
            connection.exec_driver_sql("DELETE FROM aetp_schema_version")
            connection.execute(
                text("INSERT INTO aetp_schema_version (version) VALUES (:version)"),
                {"version": SCHEMA_VERSION},
            )
        logger.info("数据库基线初始化完成: schema=%s", SCHEMA_VERSION)
        return [f"schema baseline {SCHEMA_VERSION}"]

    # ---- Session 管理 ----

    def session(self) -> Session:
        return self._session_factory()

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        session: Session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self.engine.dispose()
