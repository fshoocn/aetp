"""平台管理员审批 API 集成测试。"""

from __future__ import annotations


def _admin_headers(client) -> dict[str, str]:
    """在隔离测试数据库中创建管理员并返回认证请求头。"""
    service = client.app.state.container.auth_service()
    assert service.bootstrap_admin("admin", "admin-pass-123", "Platform Admin")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin-pass-123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_admin_can_approve_pending_user(client):
    """管理员可查看 pending 用户并将其审批为 active。"""
    admin_headers = _admin_headers(client)

    response = client.post(
        "/api/v1/auth/register",
        json={"username": "pending-user", "password": "user-pass-123"},
    )
    assert response.status_code == 201
    pending_user_id = response.json()["id"]

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "pending-user", "password": "user-pass-123"},
    )
    assert response.status_code == 403

    response = client.get(
        "/api/v1/users?account_status=pending",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert any(user["id"] == pending_user_id for user in response.json())

    response = client.patch(
        f"/api/v1/users/{pending_user_id}",
        headers=admin_headers,
        json={"account_status": "active"},
    )
    assert response.status_code == 200
    assert response.json()["account_status"] == "active"

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "pending-user", "password": "user-pass-123"},
    )
    assert response.status_code == 200


def test_bootstrap_admin_is_idempotent(client):
    """重复执行 bootstrap 不会覆盖或重复创建首个管理员。"""
    service = client.app.state.container.auth_service()

    assert service.bootstrap_admin("admin", "admin-pass-123", "Platform Admin")
    assert not service.bootstrap_admin("admin", "another-pass-456", "Other Name")

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin-pass-123"},
    )
    assert response.status_code == 200


def test_non_admin_cannot_access_admin_api(client, auth_header):
    """普通用户访问管理员 API 时返回 403。"""
    response = client.get("/api/v1/users", headers=auth_header)
    assert response.status_code == 403
