"""AuthService unit and integration tests."""
from __future__ import annotations


def test_create_user_and_authenticate(client, auth_token, auth_header):
    resp = client.get("/api/v1/auth/me", headers=auth_header)
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "tester"
    assert data["platform_role"] == "user"
    assert data["account_status"] == "active"


def test_authenticate_wrong_password(client):
    client.post("/api/v1/auth/register", json={"username": "u2", "password": "pass123456"})
    resp = client.post("/api/v1/auth/login", json={"username": "u2", "password": "wrong-password"})
    assert resp.status_code == 401


def test_authenticate_nonexistent_user(client):
    resp = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "whatever"})
    assert resp.status_code == 401


def test_duplicate_registration(client):
    client.post("/api/v1/auth/register", json={"username": "dup", "password": "pass123456"})
    resp = client.post("/api/v1/auth/register", json={"username": "dup", "password": "another"})
    assert resp.status_code == 409


def test_me_requires_auth(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401


def test_me_invalid_token(client):
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
    assert resp.status_code == 401


def test_me_rejects_non_active_account(client, auth_token, auth_header):
    """账户状态变为 disabled 或 pending 后，旧令牌也不能继续访问。"""
    container = client.app.state.container

    with container.database().session_scope() as session:
        from sqlalchemy import select, update

        from master.adapters.sqlalchemy.orm import User

        user = session.execute(
            select(User).where(User.username == "tester")
        ).scalar_one()
        session.execute(
            update(User)
            .where(User.id == user.id)
            .values(account_status="disabled")
        )

    response = client.get("/api/v1/auth/me", headers=auth_header)
    assert response.status_code == 401

    with container.database().session_scope() as session:
        session.execute(
            update(User)
            .where(User.username == "tester")
            .values(account_status="pending")
        )

    response = client.get("/api/v1/auth/me", headers=auth_header)
    assert response.status_code == 401


def test_change_password(client):
    container = client.app.state.container
    svc = container.auth_service()
    svc.create_user("cpw", "pass123456", "CPW")
    with container.database().session_scope() as s:
        from sqlalchemy import text as sa_text
        s.execute(sa_text("UPDATE users SET account_status='active' WHERE username='cpw'"))
    assert svc.authenticate("cpw", "pass123456") is not None
    assert svc.change_password("cpw", "newpass123")
    assert svc.authenticate("cpw", "newpass123") is not None
    assert svc.authenticate("cpw", "oldpass123") is None
