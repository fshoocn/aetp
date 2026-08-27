"""P2.9 跨项目资源访问保护测试。"""

from __future__ import annotations

from sqlalchemy import text

from master.adapters.sqlalchemy.orm import Device, Node


def _active_user(client, username: str) -> tuple[int, dict[str, str]]:
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


def _project_with_device(client, user_id: int, key: str) -> str:
    container = client.app.state.container
    project = container.project_service().create(
        project_key=key,
        name=key,
        description="",
        created_by=user_id,
        owner_id=user_id,
    )
    with container.database().session_scope() as session:
        node = Node(
            node_id=f"{key}-node",
            name=f"{key} Node",
            hostname=f"{key}.local",
            status="offline",
            online=False,
            enabled=True,
        )
        session.add(node)
        session.flush()
        session.add(
            Device(
                device_id=f"{key}-device",
                node_pk=node.id,
                name=f"{key} Device",
                status="offline",
                online=False,
            )
        )
    container.project_node_binding_service().bind_node(
        project.project_id,
        node_id=f"{key}-node",
        assigned_by=user_id,
    )
    return project.project_id


def test_project_resources_are_isolated(client):
    """两个项目的设备不能跨项目读取，旧任务入口已移除。"""
    user_a_id, user_a_headers = _active_user(client, "scope-user-a")
    user_b_id, user_b_headers = _active_user(client, "scope-user-b")
    project_a = _project_with_device(client, user_a_id, "SCOPE_A")
    project_b = _project_with_device(client, user_b_id, "SCOPE_B")

    assert client.get(f"/api/v1/projects/{project_a}/tasks", headers=user_a_headers).status_code == 404
    assert client.get(f"/api/v1/projects/{project_b}/tasks", headers=user_b_headers).status_code == 404

    response = client.get(
        f"/api/v1/projects/{project_a}/devices",
        headers=user_b_headers,
    )
    assert response.status_code == 404

    response = client.get(
        f"/api/v1/projects/{project_b}/devices",
        headers=user_a_headers,
    )
    assert response.status_code == 404


def test_project_scoped_device_and_task_paths_are_required(client, auth_header):
    """任务必须带 project_id；Device 全局只读接口可以访问。"""
    assert client.get("/api/v1/tasks", headers=auth_header).status_code == 404
    assert client.get("/api/v1/devices", headers=auth_header).status_code == 200
