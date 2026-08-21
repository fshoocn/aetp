"""P2.7 项目节点绑定 API 测试。"""

from __future__ import annotations

from sqlalchemy import select, text

from master.adapters.sqlalchemy.orm import Device, Node


def _create_admin(client) -> dict[str, str]:
    service = client.app.state.container.auth_service()
    assert service.bootstrap_admin("node-admin", "admin-pass-123", "Node Admin")
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "node-admin", "password": "admin-pass-123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_active_user(client, username: str) -> int:
    service = client.app.state.container.auth_service()
    user = service.create_user(username, "user-pass-123", username)
    with client.app.state.container.database().session_scope() as session:
        session.execute(
            text("UPDATE users SET account_status='active' WHERE username=:username"),
            {"username": username},
        )
    return user.id


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "user-pass-123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_project(client, headers: dict[str, str], key: str = "NODES") -> str:
    response = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert response.status_code == 201
    return response.json()["project_id"]


def _create_device(client, node_id: str) -> None:
    device_id = f"{node_id}-device"
    with client.app.state.container.database().session_scope() as session:
        node = Node(
            node_id=node_id,
            name=f"节点 {node_id}",
            hostname=f"{node_id}.local",
            status="offline",
            online=False,
            enabled=True,
        )
        session.add(node)
        session.flush()
        session.add(
            Device(
                device_id=device_id,
                node_pk=node.id,
                name=f"设备 {device_id}",
                status="offline",
                online=False,
            )
        )


def _add_device_to_node(client, node_id: str, device_id: str) -> None:
    """向已有 Node 添加第二个外设。"""
    with client.app.state.container.database().session_scope() as session:
        node_pk = session.execute(select(Node.id).where(Node.node_id == node_id)).scalar_one()
        session.add(
            Device(
                device_id=device_id,
                node_pk=node_pk,
                name=f"设备 {device_id}",
                status="offline",
                online=False,
            )
        )


def test_admin_can_bind_update_list_and_unbind_node(client):
    """平台管理员可以完成节点绑定、禁用、查询和解绑。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers)
    _create_device(client, "node-001")
    _add_device_to_node(client, "node-001", "node-001-device-2")

    response = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        headers=headers,
        json={"node_id": "node-001"},
    )
    assert response.status_code == 201
    binding = response.json()
    assert binding["node_id"] == "node-001"
    assert binding["enabled"] is True
    assert binding["name"] == "节点 node-001"
    assert binding["online"] is False
    assert binding["devices"][0]["device_id"] == "node-001-device"
    assert [device["device_id"] for device in binding["devices"]] == ["node-001-device", "node-001-device-2"]

    response = client.get(f"/api/v1/projects/{project_id}/nodes", headers=headers)
    assert response.status_code == 200
    assert len(response.json()) == 1

    response = client.patch(
        f"/api/v1/projects/{project_id}/nodes/node-001",
        headers=headers,
        json={"enabled": False},
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False

    response = client.delete(
        f"/api/v1/projects/{project_id}/nodes/node-001",
        headers=headers,
    )
    assert response.status_code == 204

    response = client.get(f"/api/v1/projects/{project_id}/nodes", headers=headers)
    assert response.status_code == 200
    assert response.json() == []


def test_maintainer_can_manage_nodes(client):
    """项目 maintainer 可以绑定和禁用节点。"""
    admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "MAINTAINER_NODES")
    maintainer_id = _create_active_user(client, "node-maintainer")
    _create_device(client, "node-002")

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": maintainer_id, "project_role": "maintainer"},
    )
    assert response.status_code == 201
    maintainer_headers = _login(client, "node-maintainer")

    response = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        headers=maintainer_headers,
        json={"node_id": "node-002"},
    )
    assert response.status_code == 201

    response = client.patch(
        f"/api/v1/projects/{project_id}/nodes/node-002",
        headers=maintainer_headers,
        json={"enabled": False},
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_viewer_can_list_but_cannot_manage_nodes(client):
    """项目 viewer 可以查询节点，但不能绑定或修改节点。"""
    admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "VIEWER_NODES")
    viewer_id = _create_active_user(client, "node-viewer")
    _create_device(client, "node-003")

    response = client.post(
        f"/api/v1/projects/{project_id}/members",
        headers=admin_headers,
        json={"user_id": viewer_id, "project_role": "viewer"},
    )
    assert response.status_code == 201
    response = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        headers=admin_headers,
        json={"node_id": "node-003"},
    )
    assert response.status_code == 201
    viewer_headers = _login(client, "node-viewer")

    response = client.get(f"/api/v1/projects/{project_id}/nodes", headers=viewer_headers)
    assert response.status_code == 200
    assert response.json()[0]["node_id"] == "node-003"

    response = client.patch(
        f"/api/v1/projects/{project_id}/nodes/node-003",
        headers=viewer_headers,
        json={"enabled": False},
    )
    assert response.status_code == 403


def test_binding_rejects_unknown_and_duplicate_nodes(client):
    """不存在节点和重复绑定分别返回 404、409。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "NODE_ERRORS")

    response = client.post(
        f"/api/v1/projects/{project_id}/nodes",
        headers=headers,
        json={"node_id": "missing-node"},
    )
    assert response.status_code == 404

    _create_device(client, "node-004")
    payload = {"node_id": "node-004"}
    assert client.post(f"/api/v1/projects/{project_id}/nodes", headers=headers, json=payload).status_code == 201
    response = client.post(f"/api/v1/projects/{project_id}/nodes", headers=headers, json=payload)
    assert response.status_code == 409


def test_non_member_cannot_read_project_nodes(client):
    """非项目成员不能读取项目节点绑定。"""
    admin_headers = _create_admin(client)
    project_id = _create_project(client, admin_headers, "PRIVATE_NODES")
    _create_device(client, "node-005")
    assert (
        client.post(
            f"/api/v1/projects/{project_id}/nodes",
            headers=admin_headers,
            json={"node_id": "node-005"},
        ).status_code
        == 201
    )

    _create_active_user(client, "node-outsider")
    outsider_headers = _login(client, "node-outsider")
    response = client.get(f"/api/v1/projects/{project_id}/nodes", headers=outsider_headers)
    assert response.status_code == 404
