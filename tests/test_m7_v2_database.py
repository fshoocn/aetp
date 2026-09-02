"""M7-1：V2-only 数据库和运行目录隔离测试。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from master import config
from master.adapters.sqlalchemy.database_factory import create_database
from master.adapters.sqlalchemy.v2_schema import LEGACY_TABLE_NAMES, V2_TABLE_NAMES
from master.config import MasterSettings


def test_v2_profile_creates_fresh_database_without_legacy_tables(tmp_path: Path) -> None:
    env_file = tmp_path / "master-v2.env"
    database_path = (tmp_path / "v2.db").as_posix()
    env_file.write_text(
        "AETP_MASTER_PROFILE=v2\n"
        f"AETP_MASTER_DATABASE_URL=sqlite:///{database_path}\n"
        "AETP_MASTER_JWT_SECRET=test-secret-at-least-32-bytes-long-for-v2\n",
        encoding="utf-8",
    )

    settings = MasterSettings.from_env_file(env_file)
    assert settings.v2_only is True
    assert settings.data_dir == (tmp_path / "data-v2").resolve()
    assert settings.database_url == f"sqlite:///{database_path}"

    config.reset_settings()
    config.configure(env_file)
    database = create_database({"url": settings.database_url, "v2_only": True})
    try:
        assert database.connect() == ["v2 schema baseline 1"]
        names = set(inspect(database.engine).get_table_names())
    finally:
        database.close()

    assert names.isdisjoint(LEGACY_TABLE_NAMES)
    assert names >= V2_TABLE_NAMES
    assert "aetp_v2_schema_version" in names
    assert "alembic_version" not in names
