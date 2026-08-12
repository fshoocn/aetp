"""P2.5 项目 CRUD API 测试。"""

from __future__ import annotations


def _admin_headers(client) -> dict[str, str]:
    """创建隔离测试用平台管理员并返回认证请求头。"""
    service = client.app.state.container.auth_service()
    assert service.bootstrap_admin("project-admin", "admin-pass-123", "Project Admin")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "project-admin", "password": "admin-pass-123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_admin_can_create_list_get_and_update_project(client):
    """平台管理员可以完成项目的创建、查询和修改。"""
    headers = _admin_headers(client)

    response = client.post(
        "/api/v1/projects",
        headers=headers,
        json={
            "project_key": "AETP_CORE",
            "name": "AETP Core",
            "description": "核心测试项目",
        },
    )
    assert response.status_code == 201
    project = response.json()
    assert project["project_id"].startswith("P-")
    assert project["project_key"] == "AETP_CORE"
    assert project["name"] == "AETP Core"
    assert project["status"] == "active"
    assert project["created_by"] == 1
    project_id = project["project_id"]

    response = client.get("/api/v1/projects", headers=headers)
    assert response.status_code == 200
    assert [item["project_id"] for item in response.json()] == [project_id]

    response = client.get(f"/api/v1/projects/{project_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["project_key"] == "AETP_CORE"

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        headers=headers,
        json={"name": "AETP Core Updated", "status": "archived"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "AETP Core Updated"
    assert response.json()["status"] == "archived"


def test_project_key_must_be_unique(client):
    """重复 project_key 返回 409。"""
    headers = _admin_headers(client)
    payload = {"project_key": "DUPLICATE", "name": "First"}

    assert client.post("/api/v1/projects", headers=headers, json=payload).status_code == 201
    response = client.post(
        "/api/v1/projects",
        headers=headers,
        json={**payload, "name": "Second"},
    )
    assert response.status_code == 409


def test_non_admin_cannot_create_or_update_project(client, auth_header):
    """普通 active 用户不能创建或修改项目。"""
    response = client.post(
        "/api/v1/projects",
        headers=auth_header,
        json={"project_key": "USER_PROJECT", "name": "User Project"},
    )
    assert response.status_code == 403


def test_non_member_cannot_see_project(client):
    """普通用户不是项目成员时不能读取管理员创建的项目。"""
    admin_headers = _admin_headers(client)
    response = client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={"project_key": "PRIVATE", "name": "Private Project"},
    )
    assert response.status_code == 201
    project_id = response.json()["project_id"]

    service = client.app.state.container.auth_service()
    service.create_user("regular-user", "user-pass-123", "Regular User")
    with client.app.state.container.database().session_scope() as session:
        from sqlalchemy import text

        session.execute(
            text(
                "UPDATE users SET account_status='active' "
                "WHERE username='regular-user'"
            )
        )
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "regular-user", "password": "user-pass-123"},
    )
    assert response.status_code == 200
    user_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    response = client.get("/api/v1/projects", headers=user_headers)
    assert response.status_code == 200
    assert response.json() == []

    response = client.get(f"/api/v1/projects/{project_id}", headers=user_headers)
    assert response.status_code == 404


def test_project_owner_can_view_project(client):
    """管理员指定普通用户为首个 owner 后，该用户可以查看项目。"""
    admin_headers = _admin_headers(client)
    service = client.app.state.container.auth_service()
    owner = service.create_user("project-owner", "owner-pass-123", "Project Owner")
    with client.app.state.container.database().session_scope() as session:
        from sqlalchemy import text

        session.execute(
            text(
                "UPDATE users SET account_status='active' "
                "WHERE username='project-owner'"
            )
        )

    response = client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={
            "project_key": "OWNER_PROJECT",
            "name": "Owner Project",
            "owner_id": owner.id,
        },
    )
    assert response.status_code == 201
    project_id = response.json()["project_id"]

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "project-owner", "password": "owner-pass-123"},
    )
    assert response.status_code == 200
    owner_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    response = client.get("/api/v1/projects", headers=owner_headers)
    assert response.status_code == 200
    assert [project["project_id"] for project in response.json()] == [project_id]

    response = client.get(f"/api/v1/projects/{project_id}", headers=owner_headers)
    assert response.status_code == 200
    assert response.json()["name"] == "Owner Project"


def test_projects_require_authentication(client):
    """项目列表和创建接口都需要认证。"""
    response = client.get("/api/v1/projects")
    assert response.status_code == 401

    response = client.post(
        "/api/v1/projects",
        json={"project_key": "NO_AUTH", "name": "No Auth"},
    )
    assert response.status_code == 401
