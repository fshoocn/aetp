"""已废弃 tasks 入口的契约测试。"""

from __future__ import annotations

import pytest

from master.adapters.sqlalchemy.orm import Device, Node


@pytest.fixture
def task_context(client, auth_token):
    """创建测试用户可访问的项目、Node、Device 和绑定。"""
    auth_header = {"Authorization": f"Bearer {auth_token}"}
    user_id = client.get("/api/v1/auth/me", headers=auth_header).json()["id"]
    container = client.app.state.container

    project = container.project_service().create(
        project_key="TASK_PROJECT",
        name="Task Project",
        description="",
        created_by=user_id,
        owner_id=user_id,
    )
    with container.database().session_scope() as session:
        node = Node(
            node_id="task-node",
            name="Task Node",
            hostname="task-node.local",
            status="offline",
            online=False,
            enabled=True,
        )
        session.add(node)
        session.flush()
        session.add(
            Device(
                device_id="task-device",
                node_pk=node.id,
                name="Task Device",
                status="offline",
                online=False,
            )
        )
    container.project_node_binding_service().bind_node(
        project.project_id,
        node_id="task-node",
        assigned_by=user_id,
    )
    return project.project_id, auth_header


def test_legacy_project_tasks_entry_is_removed(client, task_context):
    project_id, auth_header = task_context
    base = f"/api/v1/projects/{project_id}/tasks"

    assert client.get(base, headers=auth_header).status_code == 404
    assert client.post(
        base,
        json={"device_id": "task-device", "command": {"test": "vibration"}},
        headers=auth_header,
    ).status_code == 405


def test_legacy_project_task_detail_entry_is_removed(client, task_context):
    project_id, auth_header = task_context
    resp = client.get(
        f"/api/v1/projects/{project_id}/tasks/T-NOEXIST",
        headers=auth_header,
    )
    assert resp.status_code == 404


def test_tasks_require_auth(client):
    resp = client.get("/api/v1/projects/unknown/tasks")
    assert resp.status_code == 404


def test_global_task_endpoint_is_removed(client, auth_header):
    """任务必须带项目范围，旧全局入口不再提供普通用户访问。"""
    resp = client.get("/api/v1/tasks", headers=auth_header)
    assert resp.status_code == 404


