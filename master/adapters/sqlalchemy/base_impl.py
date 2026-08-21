"""SQLAlchemy 通用实现基类。

封装所有数据库共用的逻辑：
- 根据连接串创建 engine（驱动补全由子类负责）；
- 通过 Alembic 执行显式数据库迁移（线程安全，见 _MIGRATION_LOCK）；
- Session 工厂与会话上下文。

各具体数据库实现（sqlite / mysql / postgresql ...）继承本类，
只需提供驱动名与各自的 engine 定制。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from master.config import PROJECT_ROOT

from .database_interface import DatabaseConfig, DatabaseInterface

logger = logging.getLogger(__name__)

# 迁移互斥锁：防止同一进程内多个线程并发执行 Alembic 迁移
_MIGRATION_LOCK = threading.Lock()


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

    # ---- 自动迁移 ----

    def connect(self) -> list[str]:
        """建立连接并执行自动迁移（Alembic upgrade head）。"""
        return self._run_migrations()

    def sync_schema(self) -> list[str]:
        """同 connect()，执行 Alembic 迁移。"""
        return self._run_migrations()

    def _run_migrations(self) -> list[str]:
        from master.config import get_settings

        try:
            settings = get_settings()
        except RuntimeError:
            # 配置未初始化（如测试或命令行仅创建数据库对象），跳过自动迁移。
            logger.debug("配置未初始化，跳过数据库自动迁移")
            return ["(no settings, skipping auto-migrate)"]
        if not settings.auto_migrate:
            logger.info("auto_migrate 已关闭，跳过自动迁移")
            return ["(auto_migrate disabled)"]

        with _MIGRATION_LOCK:
            from alembic import command
            from alembic.config import Config as AlembicConfig

            alembic_ini = PROJECT_ROOT / "alembic.ini"
            aleb_cfg = AlembicConfig(str(alembic_ini))
            # 复用当前应用 Engine 的连接，避免 Alembic 再创建第二个 Engine。
            logger.info("开始数据库迁移: database_type=%s", self.db_type)
            with self.engine.begin() as connection:
                aleb_cfg.attributes["connection"] = connection
                command.upgrade(aleb_cfg, "head")
            logger.info("Alembic upgrade head 完成")
            return ["alembic upgrade head"]

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
