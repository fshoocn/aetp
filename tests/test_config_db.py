"""配置与健康检查测试。"""

from __future__ import annotations


def test_health_check(client):
    """健康检查端点应返回 200。"""
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert client.get("/api/health").status_code == 404


def test_config_singleton_mode():
    """验证配置组合根行为：首次加载后 get_settings() 返回相同实例。"""
    from master import config

    config.reset_settings()
    s1 = config.configure()
    s2 = config.get_settings()
    assert s1 is s2
    config.reset_settings()


def test_database_sqlite_url_resolution():
    """验证 SQLite 驱动匹配与 engine 创建。"""
    from master.adapters.sqlalchemy.database_factory import create_database

    db = create_database("sqlite:///:memory:")
    db.connect()
    assert db.db_type == "sqlite"
    db.close()


def test_database_connect_skips_migration_without_configuration():
    """未初始化进程配置时，数据库对象应明确跳过自动迁移。"""
    from master import config
    from master.adapters.sqlalchemy.database_factory import create_database

    config.reset_settings()
    db = create_database("sqlite:///:memory:")
    assert db.connect() == ["(no settings, skipping auto-migrate)"]
    db.close()


def test_database_driver_resolution():
    """验证自动补全数据库驱动名称。"""
    from master.adapters.sqlalchemy.database_factory import _detect_scheme

    assert _detect_scheme("mysql+pymysql://u:p@h/db") == "mysql"
    assert _detect_scheme("postgresql://u:p@h/db") == "postgresql"
    assert _detect_scheme("sqlite:///data/aetp.db") == "sqlite"


def test_sqlite_path_resolution_is_shared():
    """应用数据库层和迁移层应使用同一套 SQLite 相对路径规则。"""
    from master.adapters.sqlalchemy.sqlite_impl import SQLiteDatabase
    from master.config import resolve_sqlite_url

    url = "sqlite:///data/aetp.db"
    assert SQLiteDatabase._resolve_path(url) == resolve_sqlite_url(url)
    driver_url = "sqlite+pysqlite:///data/aetp.db"
    assert SQLiteDatabase._resolve_path(driver_url) == resolve_sqlite_url(driver_url)


def test_env_isolation():
    """验证配置不从系统环境变量读取，仅从文件读取。"""
    import os
    import tempfile
    from pathlib import Path

    from master.config import MasterSettings

    os.environ["AETP_MASTER_MQTT_HOST"] = "wrong-from-env"
    env = Path(tempfile.mkdtemp()) / "test.env"
    env.write_text("AETP_MASTER_MQTT_HOST=from-file\n", encoding="utf-8")
    s = MasterSettings.from_env_file(env)
    assert s.mqtt_host == "from-file"
    assert s.mqtt_host != "wrong-from-env"
    env.unlink()


def test_case_duration_settings_are_loaded_from_env(tmp_path):
    """P6.8 默认耗时和异常阈值来自 Master 外置配置。"""
    from master.config import MasterSettings

    env = tmp_path / "duration.env"
    env.write_text(
        "AETP_MASTER_CASE_DURATION_DEFAULT_S=45.5\n"
        "AETP_MASTER_CASE_DURATION_ANOMALY_PERCENT=75\n",
        encoding="utf-8",
    )
    settings = MasterSettings.from_env_file(env)
    assert settings.case_duration_default_s == 45.5
    assert settings.case_duration_anomaly_percent == 75.0
