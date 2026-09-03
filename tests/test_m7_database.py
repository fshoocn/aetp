"""数据库基线和运行目录隔离测试。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from master import config
from master.adapters.sqlalchemy.database_factory import create_database
from master.adapters.sqlalchemy.schema import TABLE_NAMES
from master.config import MasterSettings


def test_fresh_database_uses_current_schema(tmp_path: Path) -> None:
    env_file = tmp_path / "master.env"
    database_path = (tmp_path / "master.db").as_posix()
    env_file.write_text(
        ""
        f"AETP_MASTER_DATABASE_URL=sqlite:///{database_path}\n"
        "AETP_MASTER_JWT_SECRET=test-secret-at-least-32-bytes-long\n",
        encoding="utf-8",
    )

    settings = MasterSettings.from_env_file(env_file)
    assert settings.data_dir == (tmp_path / "data").resolve()
    assert settings.database_url == f"sqlite:///{database_path}"

    config.reset_settings()
    config.configure(env_file)
    database = create_database(settings.database_url)
    try:
        assert database.connect() == ["schema baseline 1"]
        names = set(inspect(database.engine).get_table_names())
    finally:
        database.close()

    assert names >= TABLE_NAMES
    assert "aetp_schema_version" in names
    assert "alembic_version" not in names
