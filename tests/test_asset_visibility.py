"""Node/Device 全局只读可见性与任务权限测试。"""

from __future__ import annotations

from sqlalchemy import text

from master.adapters.sqlalchemy.orm import Device, Node


def _login_user(client, username: str) -> tuple[int, dict[str, str]]:
    service = client.app.state.container.auth_service()
    user = service.create_user(username, "user-pass-123", username)
    with client.app.state.container.database().session_scope() as session:
        session.execute(
            text("UPDATE users SET account_status='active' WHERE username=:username"),
            {"username": username},
        )
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "user-pass-123"},
    )
    assert response.status_code == 200
    return user.id, {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_admin(client) -> dict[str, str]:
    service = client.app.state.container.auth_service()
    assert service.bootstrap_admin("asset-admin", "admin-pass-123", "Asset Admin")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "asset-admin", "password": "admin-pass-123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_asset(client) -> None:
    with client.app.state.container.database().session_scope() as session:
        node = Node(
            node_id="global-node",
            name="Global Node",
            hostname="global.local",
            status="online",
            online=True,
            enabled=True,
        )
        session.add(node)
        session.flush()
        session.add(
            Device(
                device_id="global-device",
                node_pk=node.id,
                name="Global Device",
                status="online",
                online=True,
            )
        )


def test_active_users_can_view_all_nodes_and_devices(client, auth_header):
    """普通 active 用户可以查看平台 Node/Device 资产。"""
    _create_asset(client)

    response = client.get("/api/v1/nodes", headers=auth_header)
    assert response.status_code == 200
    assert response.json()[0]["node_id"] == "global-node"
    assert response.json()[0]["devices"][0]["device_id"] == "global-device"

    response = client.get("/api/v1/nodes/global-node", headers=auth_header)
    assert response.status_code == 200

    response = client.get("/api/v1/devices", headers=auth_header)
    assert response.status_code == 200
    assert response.json()[0]["node_id"] == "global-node"

    response = client.get("/api/v1/devices/global-device", headers=auth_header)
    assert response.status_code == 200


def test_viewer_can_view_assets_and_test_task_definitions_but_cannot_create(client):
    """viewer 可以查看资产和任务定义，但不能创建任务定义。"""
    admin_headers = _create_admin(client)
    viewer_id, viewer_headers = _login_user(client, "asset-viewer")
    _create_asset(client)

    project = client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={
            "project_key": "ASSET_PERMISSION",
            "name": "Asset Permission",
        },
    )
    assert project.status_code == 201
    project_id = project.json()["project_id"]

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": viewer_id, "project_role": "viewer"},
    )
    assert response.status_code == 201

    response = client.get("/api/v1/nodes", headers=viewer_headers)
    assert response.status_code == 200

    response = client.get(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=viewer_headers,
    )
    assert response.status_code == 200

    response = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=viewer_headers,
        json={"name": "viewer-task", "script_id": "missing-script"},
    )
    assert response.status_code == 403

    response = client.get(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=admin_headers,
    )
    assert response.status_code == 200
