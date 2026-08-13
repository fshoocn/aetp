"""P3.1：任务状态命名迁移（Alembic 0003）往返测试。

验证 upgrade 数据回填与 CHECK 重建、downgrade 可回滚
（cancelling -> cancelled 为有损合并，已在迁移注释说明）。
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text as sa_text

import master.config as config


def _alembic_config() -> Config:
    cfg = Config(str(config.PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(config.PROJECT_ROOT / "migrations")
    )
    return cfg


def _insert_legacy_rows(db_path: str) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                sa_text(
                    "INSERT INTO users (id, username, password_hash, display_name,"
                    " account_status, platform_role, created_at, updated_at)"
                    " VALUES (1, 'u', 'h', '', 'active', 'user',"
                    " '2026-08-13 00:00:00', '2026-08-13 00:00:00')"
                )
            )
            conn.execute(
                sa_text(
                    "INSERT INTO projects (id, project_id, project_key, name,"
                    " description, status, created_by, created_at, updated_at)"
                    " VALUES (1, 'p1', 'P1', 'p', '', 'active', 1,"
                    " '2026-08-13 00:00:00', '2026-08-13 00:00:00')"
                )
            )
            conn.execute(
                sa_text(
                    "INSERT INTO devices (id, device_id, name, status, online,"
                    " created_at, updated_at)"
                    " VALUES (1, 'd1', 'd', 'online', 1,"
                    " '2026-08-13 00:00:00', '2026-08-13 00:00:00')"
                )
            )
            for status in (
                "pending",
                "dispatched",
                "accepted",
                "running",
                "completed",
                "failed",
                "cancelled",
                "timeout",
            ):
                conn.execute(
                    sa_text(
                        "INSERT INTO tasks (task_id, project_pk, device_pk,"
                        " created_by, status, command, created_at, updated_at)"
                        " VALUES (:tid, 1, 1, 1, :st, '{}',"
                        " '2026-08-13 00:00:00', '2026-08-13 00:00:00')"
                    ),
                    {"tid": f"T-{status}", "st": status},
                )
    finally:
        engine.dispose()


def _read_statuses(db_path: str) -> dict[str, str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa_text("SELECT task_id, status FROM tasks")
            ).all()
            # 用下标显式构建，避免 dict(Row) 触发 Pylance 的 __init__ 重载误报
            return {row[0]: row[1] for row in rows}
    finally:
        engine.dispose()


def test_task_status_rename_roundtrip(tmp_path):
    db_path = tmp_path / "migrate.db"
    env = tmp_path / "migrate.env"
    env.write_text(
        f"AETP_MASTER_DATABASE_URL=sqlite:///{db_path}\n"
        "AETP_MASTER_JWT_SECRET=test-secret-at-least-32-bytes-long-for-pytest\n",
        encoding="utf-8",
    )
    config.reset_settings()
    config.configure(env)

    cfg = _alembic_config()
    # 先升级到 0003 之前（0002_refresh_tokens），此时 tasks 仍使用旧状态命名
    command.upgrade(cfg, "0002_refresh_tokens")
    _insert_legacy_rows(db_path)

    # 升级到 head：数据回填 + CHECK 重建
    command.upgrade(cfg, "head")
    rows = _read_statuses(db_path)
    assert rows["T-pending"] == "pending"
    assert rows["T-dispatched"] == "dispatching"
    assert rows["T-accepted"] == "dispatching"
    assert rows["T-running"] == "running"
    assert rows["T-completed"] == "succeeded"
    assert rows["T-failed"] == "failed"
    assert rows["T-cancelled"] == "cancelled"
    assert rows["T-timeout"] == "timed_out"

    # 降级回 0002：新值还原为旧值（可回滚）
    command.downgrade(cfg, "0002_refresh_tokens")
    rows = _read_statuses(db_path)
    assert rows["T-dispatched"] == "dispatched"
    assert rows["T-completed"] == "completed"
    assert rows["T-timeout"] == "timeout"
