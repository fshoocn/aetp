"""刷新令牌会话测试（P2.10）与令牌响应契约（P2.9）。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text as sa_text

from master.api.v1.security import hash_refresh_token
from master.domain.time import utcnow

TEST_PASSWORD = "**********"


def _login(client, username="tester", password=TEST_PASSWORD) -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_login_returns_token_pair(client, auth_token):
    data = _login(client)
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0
    assert data["refresh_token"] != data["access_token"]


def test_refresh_rotates_token_and_revokes_old(client, auth_token):
    data = _login(client)
    old_refresh = data["refresh_token"]

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 200, resp.text
    new_data = resp.json()
    assert new_data["refresh_token"] != old_refresh
    assert new_data["access_token"]

    # 旧刷新令牌已撤销，重放应被拒绝
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 401

    # 新令牌仍可用
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": new_data["refresh_token"]})
    assert resp.status_code == 200


def test_refresh_unknown_token_rejected(client):
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": "unknown-token-value-123456"})
    assert resp.status_code == 401


def test_logout_revokes_refresh_token(client, auth_token):
    data = _login(client)
    resp = client.post("/api/v1/auth/logout", json={"refresh_token": data["refresh_token"]})
    assert resp.status_code == 204

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert resp.status_code == 401


def test_change_password_revokes_refresh_tokens(client, auth_token):
    container = client.app.state.container
    svc = container.auth_service()
    data = _login(client, username="tester")

    assert svc.change_password("tester", "newpass123")

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert resp.status_code == 401

    # 新密码登录正常，且新会话可刷新
    new_data = _login(client, username="tester", password="newpass123")
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": new_data["refresh_token"]})
    assert resp.status_code == 200


def test_disabled_user_cannot_refresh(client, auth_token):
    container = client.app.state.container
    data = _login(client)

    with container.database().session_scope() as s:
        s.execute(sa_text("UPDATE users SET account_status='disabled' WHERE username='tester'"))

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert resp.status_code == 401

    with container.database().session_scope() as s:
        s.execute(sa_text("UPDATE users SET account_status='active' WHERE username='tester'"))


def test_expired_refresh_token_rejected(client, auth_token):
    container = client.app.state.container
    svc = container.auth_service()

    user = svc.authenticate("tester", TEST_PASSWORD)
    assert user is not None

    expired_hash = hash_refresh_token("expired-token-value-123456")
    svc.issue_refresh_token(user.id, expired_hash, utcnow() - timedelta(seconds=1))

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": "expired-token-value-123456"})
    assert resp.status_code == 401


def test_access_token_carries_iss_and_aud(client, auth_token):
    from master.api.v1.security import decode_access_token

    payload = decode_access_token(auth_token)
    assert payload["iss"] == "aetp-master"
    assert payload["aud"] == "aetp-web"
    assert "jti" in payload
