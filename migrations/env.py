"""Alembic 迁移环境配置。

应用启动时由 base_impl._run_migrations() 注入已经打开的数据库连接，
手动执行 Alembic 命令时则从 MasterSettings 获取数据库 URL 并创建临时连接。

路径说明：本项目未安装为 site-packages 包，env.py 依赖项目根目录在
sys.path 中（应用启动时天然满足；命令行执行时由 alembic.ini 的
prepend_sys_path=. 保证）。
"""

from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

from alembic import context

# ---------- 导入路径 ----------
# 项目根目录 = 本文件（migrations/env.py）的上两级
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from master.config import configure, get_settings, resolve_sqlite_url  # noqa: E402
# 导入 ORM 模型，确保所有表注册到 metadata（即表结构唯一事实源）
from master.adapters.sqlalchemy.orm.base import Base  # noqa: E402
from master.adapters.sqlalchemy import orm as _orm  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_db_url() -> str | None:
    """从进程级配置读取数据库 URL，并解析 SQLite 相对路径。"""
    try:
        url = get_settings().database_url
    except RuntimeError:
        # 配置未初始化（如直接执行 Alembic 命令），尝试初始化默认配置。
        configure()
        url = get_settings().database_url

    return resolve_sqlite_url(url)


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本而不连接数据库。"""
    url = _resolve_db_url()
    if url is None:
        raise RuntimeError("未配置数据库 URL，请先初始化 MasterSettings")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    """使用传入的数据库连接执行迁移。"""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：优先使用外部连接，否则创建临时连接。"""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return

    # 直接执行 Alembic 命令时没有应用连接，此处才创建临时 Engine。
    url = _resolve_db_url()
    if url is None:
        raise RuntimeError("未配置数据库 URL，请先初始化 MasterSettings")
    connectable = create_engine(url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            _run_migrations(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
