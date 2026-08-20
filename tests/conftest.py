"""共享 fixture：临时数据库、容器、API 客户端。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Generator, cast

from fastapi import FastAPI
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

import master.config as config
import master.main as main_mod


@pytest.fixture(autouse=True)
def _isolated_config() -> Generator[None, None, None]:
    """每个测试使用独立临时数据库 + 临时 .env + 临时数据目录，互不干扰。"""
    tmp_dir = Path(tempfile.mkdtemp())
    db_path = tmp_dir / "test.db"
    data_dir = tmp_dir / "data"
    env_file = tmp_dir / "test.env"
    env_file.write_text(
        f"AETP_MASTER_DATABASE_URL=sqlite:///{db_path}\n"
        f"AETP_MASTER_DATA_DIR={data_dir}\n"
        f"AETP_MASTER_JWT_SECRET=test-secret-at-least-32-bytes-long-for-pytest\n",
        encoding="utf-8",
    )

    config.reset_settings()
    config.configure(env_file)

    yield

    config.reset_settings()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """返回带 lifespan 的 TestClient（自动初始化 DB 与容器）。"""
    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture
def auth_token(client: TestClient) -> str:
    """注册一个已激活用户并登录，返回 JWT Bearer token。

    绕过 HTTP API 直接通过容器 auth_service 创建 + 手动激活，
    避免 register→pending→login 的审批流程干扰其他测试。
    """
    container = cast(FastAPI, client.app).state.container
    svc = container.auth_service()
    svc.create_user("tester", "**********", "Tester")
    with container.database().session_scope() as s:
        s.execute(sa_text("UPDATE users SET account_status='active' WHERE username='tester'"))
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": "tester", "password": "**********"},
    )
    assert resp.status_code == 200, f"auth_token fixture login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture
def auth_header(auth_token: str) -> dict[str, str]:
    """带 Authorization 头的请求头字典。"""
    return {"Authorization": f"Bearer {auth_token}"}
