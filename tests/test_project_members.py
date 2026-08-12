"""P2.6 项目成员 API 和项目角色权限测试。"""

from __future__ import annotations

from sqlalchemy import text


def _create_admin(client) -> tuple[int, dict[str, str]]:
    service = client.app.state.container.auth_service()
    admin = service.bootstrap_admin("member-admin", "admin-pass-123", "Member Admin")
    assert admin
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "member-admin", "password": "admin-pass-123"},
    )
    assert response.status_code == 200
    return 1, {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_active_user(client, username: str, password: str = "user-pass-123") -> int:
    service = client.app.state.container.auth_service()
    user = service.create_user(username, password, username)
    with client.app.state.container.database().session_scope() as session:
        session.execute(
            text(
                "UPDATE users SET account_status='active' "
                "WHERE username=:username"
            ),
            {"username": username},
        )
    return user.id


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_project(client, headers: dict[str, str], key: str = "MEMBERS") -> str:
    response = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert response.status_code == 201
    return response.json()["project_id"]


def test_admin_can_manage_project_members(client):
    """平台管理员可以查询、添加、修改和移除项目成员。"""
    _, admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers)
    target_id = _create_active_user(client, "member-user")

    response = client.get(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["project_role"] == "owner"

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": target_id, "project_role": "viewer"},
    )
    assert response.status_code == 201
    assert response.json()["user_id"] == target_id
    assert response.json()["project_role"] == "viewer"

    response = client.patch(
        f"/api/v1/projects/{project_id}/members/{target_id}",
        headers=admin_headers,
        json={"project_role": "operator"},
    )
    assert response.status_code == 200
    assert response.json()["project_role"] == "operator"

    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{target_id}",
        headers=admin_headers,
    )
    assert response.status_code == 204


def test_only_owner_or_admin_can_grant_owner(client):
    """maintainer 不能授予 owner，管理员可以授予第二个 owner。"""
    _, admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "OWNER_GRANT")
    maintainer_id = _create_active_user(client, "maintainer-user")
    target_id = _create_active_user(client, "target-user")

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": maintainer_id, "project_role": "maintainer"},
    )
    assert response.status_code == 201
    maintainer_headers = _login(client, "maintainer-user", "user-pass-123")

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=maintainer_headers,
        json={"user_id": target_id, "project_role": "owner"},
    )
    assert response.status_code == 403

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": target_id, "project_role": "owner"},
    )
    assert response.status_code == 201
    assert response.json()["project_role"] == "owner"


def test_maintainer_can_manage_lower_roles_only(client):
    """maintainer 可以管理 viewer/operator，但不能管理同级或更高角色。"""
    _, admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "ROLE_LEVEL")
    maintainer_id = _create_active_user(client, "maintainer-level")
    viewer_id = _create_active_user(client, "viewer-level")
    target_id = _create_active_user(client, "target-level")

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": maintainer_id, "project_role": "maintainer"},
    )
    assert response.status_code == 201
    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": viewer_id, "project_role": "viewer"},
    )
    assert response.status_code == 201

    maintainer_headers = _login(client, "maintainer-level", "user-pass-123")
    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=maintainer_headers,
        json={"user_id": target_id, "project_role": "operator"},
    )
    assert response.status_code == 201

    response = client.patch(
        f"/api/v1/projects/{project_id}/members/{viewer_id}",
        headers=maintainer_headers,
        json={"project_role": "maintainer"},
    )
    assert response.status_code == 403

    response = client.delete(
        f"/api/v1/projects/{project_id}/members/{viewer_id}",
        headers=maintainer_headers,
    )
    assert response.status_code == 204


def test_last_owner_cannot_be_removed_or_demoted(client):
    """项目最后一个 owner 不能被删除或降级。"""
    _, admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "LAST_OWNER")

    response = client.patch(
        f"/api/v1/projects/{project_id}/members/1",
        headers=admin_headers,
        json={"project_role": "maintainer"},
    )
    assert response.status_code == 409

    response = client.delete(
        f"/api/v1/projects/{project_id}/members/1",
        headers=admin_headers,
    )
    assert response.status_code == 409


def test_inactive_user_cannot_be_added(client):
    """pending 用户不能加入项目。"""
    _, admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "ACTIVE_ONLY")
    pending_id = client.app.state.container.auth_service().create_user(
        "pending-member", "user-pass-123", "Pending"
    ).id

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": pending_id, "project_role": "viewer"},
    )
    assert response.status_code == 409


def test_viewer_cannot_manage_members(client):
    """viewer 可以属于项目，但不能管理项目成员。"""
    _, admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "VIEWER_ACCESS")
    viewer_id = _create_active_user(client, "member-viewer")
    target_id = _create_active_user(client, "member-target")

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": viewer_id, "project_role": "viewer"},
    )
    assert response.status_code == 201
    viewer_headers = _login(client, "member-viewer", "user-pass-123")

    response = client.get(
        f"/api/v1/projects/{project_id}/members",
        headers=viewer_headers,
    )
    assert response.status_code == 403

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=viewer_headers,
        json={"user_id": target_id, "project_role": "viewer"},
    )
    assert response.status_code == 403
