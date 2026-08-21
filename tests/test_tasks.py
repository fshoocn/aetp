"""项目范围 TaskService 与任务 API 测试。"""

from __future__ import annotations

import re

import pytest

from master.adapters.sqlalchemy.orm import Device, Node

_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _is_ulid(value: str) -> bool:
    return bool(_ULID_RE.match(value))


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


def test_create_and_list_tasks(client, task_context):
    project_id, auth_header = task_context
    base = f"/api/v1/projects/{project_id}/tasks"

    resp = client.post(
        base,
        json={"device_id": "task-device", "command": {"test": "vibration"}},
        headers=auth_header,
    )
    assert resp.status_code == 201
    data = resp.json()
    task_id = data["task_id"]
    assert data["project_id"] == project_id
    assert data["status"] == "pending"
    assert data["device_id"] == "task-device"

    resp = client.get(base, headers=auth_header)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.get(f"{base}/{task_id}", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json()["task_id"] == task_id

    resp = client.get(f"{base}/{task_id}/logs", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_nonexistent_task(client, task_context):
    project_id, auth_header = task_context
    resp = client.get(
        f"/api/v1/projects/{project_id}/tasks/T-NOEXIST",
        headers=auth_header,
    )
    assert resp.status_code == 404


def test_tasks_require_auth(client):
    resp = client.get("/api/v1/projects/unknown/tasks")
    assert resp.status_code == 401


def test_global_task_endpoint_is_removed(client, auth_header):
    """任务必须带项目范围，旧全局入口不再提供普通用户访问。"""
    resp = client.get("/api/v1/tasks", headers=auth_header)
    assert resp.status_code == 404


def test_create_task_rejects_unbound_device(client, task_context):
    project_id, auth_header = task_context
    resp = client.post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"device_id": "not-bound", "command": {"cmd": "ping"}},
        headers=auth_header,
    )
    assert resp.status_code == 404


def test_create_task_service_direct(client, task_context):
    project_id, _ = task_context
    svc = client.app.state.container.task_service()
    task = svc.create(
        project_id=project_id,
        device_id="task-device",
        command={"cmd": "ping"},
        created_by=1,
    )
    assert _is_ulid(task.task_id)
    assert task.status == "pending"
    assert task.project_id == project_id
    assert task.device_id == "task-device"
    assert task.command["cmd"] == "ping"
    found = svc.get_by_id(task.task_id, project_id=project_id)
    assert found is not None
    assert found.task_id == task.task_id
