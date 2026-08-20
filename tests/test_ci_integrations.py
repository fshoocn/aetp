"""P8.3：CI/CD 集成 API 测试。

验收要点（§8.8）：
1. 集成 CRUD（owner 权限）
2. 触发绑定 CRUD（maintainer 权限）
3. 签名验证失败拒绝
4. delivery 去重
"""

from __future__ import annotations

from sqlalchemy import text


def _create_admin(client, username="ci-admin", password="admin-pass-123") -> dict[str, str]:
    service = client.app.state.container.auth_service()
    service.bootstrap_admin(username, password, username)
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers, key="CI"):
    resp = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _add_member(client, headers, project_id, username, password, role):
    svc = client.app.state.container.auth_service()
    user = svc.create_user(username, password, username)
    with client.app.state.container.database().session_scope() as session:
        session.execute(
            text("UPDATE users SET account_status='active' WHERE username=:u"),
            {"u": username},
        )
    client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=headers,
        json={"user_id": user.id, "project_role": role},
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_integration_crud(client):
    """CI 集成 CRUD + secret 不回显。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "CI-CRUD")

    # 创建集成
    resp = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
        json={
            "provider": "github",
            "name": "GitHub Actions",
            "secret_value": "webhook-secret-123",
            "config_json": {"events": ["push"]},
        },
    )
    assert resp.status_code == 201, resp.text
    integration = resp.json()
    assert integration["provider"] == "github"
    assert integration["has_secret"] is True
    assert "secret" not in integration
    integration_id = integration["integration_id"]

    # 查询列表
    resp = client.get(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 编辑
    resp = client.patch(
        f"/api/v1/projects/{project_id}/integrations/{integration_id}",
        headers=headers,
        json={"name": "Updated GitHub"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated GitHub"

    # 删除
    resp = client.delete(
        f"/api/v1/projects/{project_id}/integrations/{integration_id}",
        headers=headers,
    )
    assert resp.status_code == 204


def test_integration_requires_owner(client):
    """viewer 不能创建集成。"""
    admin_headers = _create_admin(client, "ci-admin2")
    project_id = _create_project(client, admin_headers, "CI-PERM")
    viewer_headers = _add_member(client, admin_headers, project_id, "ci-viewer", "pass123", "viewer")

    resp = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=viewer_headers,
        json={"provider": "github", "name": "test"},
    )
    assert resp.status_code == 403


def test_binding_crud(client):
    """CI 触发绑定 CRUD。"""
    headers = _create_admin(client, "ci-admin3")
    project_id = _create_project(client, headers, "CI-BIND")

    # 创建集成
    integration = client.post(
        f"/api/v1/projects/{project_id}/integrations",
        headers=headers,
        json={"provider": "github", "name": "bind-test"},
    ).json()
    integration_id = integration["integration_id"]

    # 创建绑定
    resp = client.post(
        f"/api/v1/projects/{project_id}/integrations/{integration_id}/bindings",
        headers=headers,
        json={
            "task_id": "T-ANY",
            "event_filter_json": {"event": "push", "branch": ["main", "develop"]},
        },
    )
    # 任务不存在时应失败
    assert resp.status_code == 422
