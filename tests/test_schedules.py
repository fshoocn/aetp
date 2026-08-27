"""P8.2：任务调度计划 API 测试。

验收要点（D-18，§18.7）：
1. cron 与 interval 互斥校验
2. 调度计划 CRUD（项目 owner 创建、列表、更新、删除）
3. 无效 cron 表达式拒绝
4. viewer 不能创建调度计划
"""

from __future__ import annotations

import hashlib


def _create_admin(client, username="sched-admin", password="admin-pass-123") -> dict[str, str]:
    service = client.app.state.container.auth_service()
    service.bootstrap_admin(username, password, username)
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers, key="SCHED"):
    resp = client.post(
        "/api/v1/projects",
        headers=headers,
        json={"project_key": key, "name": key},
    )
    assert resp.status_code == 201
    return resp.json()["project_id"]


def _create_task(client, headers, project_id, name="test-task"):
    resp = client.post(
        f"/api/v1/projects/{project_id}/test-tasks",
        headers=headers,
        json={
            "name": name,
            "script_id": "dummy",
            "split_policy": {"type": "none"},
            "retry_policy": {"max_attempts": 1},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_task(client, headers, project_id, name="sched-task"):
    """直接写入脚本和任务定义，避免外部依赖。"""
    container = client.app.state.container
    suffix = name.upper().replace("-", "_")
    script_id = f"S-{suffix}"
    task_id = f"T-{suffix}"
    with container.uow_factory()() as uow:
        from master.domain.enums import ScriptParseLocation, ScriptParseStatus
        from master.domain.models import TestScript, TestTask

        uow.test_scripts.add(
            TestScript(
                project_id=project_id,
                script_id=script_id,
                task_type="pytest",
                name=f"dummy-{name}",
                version=1,
                file_ref=f"data/scripts/{script_id}/1/dummy.py",
                size=10,
                sha256=hashlib.sha256(name.encode()).hexdigest(),
                config={},
                parse_status=ScriptParseStatus.PARSED,
                parse_location=ScriptParseLocation.MASTER,
                result_parse_location=ScriptParseLocation.MASTER,
                plugin_version="1.0.0",
                created_by=1,
            )
        )
        task = uow.test_tasks.add(
            TestTask(
                task_id=task_id,
                project_id=project_id,
                script_id=script_id,
                script_version=1,
                task_type="pytest",
                name=name,
                enabled=True,
                created_by=1,
            )
        )
    return task.task_id


def test_schedule_create_requires_exactly_one(client):
    """必须提供 cron 或 interval 之一。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "SCHED-ONE")
    task_id = _seed_task(client, headers, project_id)

    # 两个都为空
    resp = client.post(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules",
        headers=headers,
        json={},
    )
    assert resp.status_code == 422
    assert "cron_expression" in resp.json()["message"]

    # 两个都提供
    resp = client.post(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules",
        headers=headers,
        json={"cron_expression": "*/5 * * * *", "interval_seconds": 60},
    )
    assert resp.status_code == 422
    assert "互斥" in resp.json()["message"]


def test_schedule_create_with_invalid_cron(client):
    """无效 cron 表达式拒绝。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "SCHED-CRON")
    task_id = _seed_task(client, headers, project_id)

    resp = client.post(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules",
        headers=headers,
        json={"cron_expression": "not-a-cron"},
    )
    assert resp.status_code == 422
    assert "cron" in resp.json()["message"].lower()


def test_schedule_create_interval(client):
    """有效 interval 创建成功。"""
    headers = _create_admin(client)
    project_id = _create_project(client, headers, "SCHED-INT")
    task_id = _seed_task(client, headers, project_id)

    resp = client.post(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules",
        headers=headers,
        json={"interval_seconds": 3600, "timezone": "Asia/Shanghai"},
    )
    assert resp.status_code == 201, resp.text
    schedule = resp.json()
    assert schedule["interval_seconds"] == 3600
    assert schedule["cron_expression"] is None
    assert schedule["next_run_at"] is not None
    assert schedule["enabled"] is True

    # 查询列表
    resp = client.get(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules",
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_schedule_resource_scope_checks_task_and_project(client):
    """Schedule 的 URL 必须同时匹配项目和任务定义。"""
    headers = _create_admin(client, username="sched-scope-admin")
    project_id = _create_project(client, headers, "SCHED-SCOPE")
    first_task_id = _seed_task(client, headers, project_id, name="first")
    second_task_id = _seed_task(client, headers, project_id, name="second")

    created = client.post(
        f"/api/v1/projects/{project_id}/tasks/{first_task_id}/schedules",
        headers=headers,
        json={"interval_seconds": 60},
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["schedule_id"]

    wrong_task = client.get(
        f"/api/v1/projects/{project_id}/tasks/{second_task_id}/schedules",
        headers=headers,
    )
    assert wrong_task.status_code == 200
    assert wrong_task.json() == []

    wrong_update = client.patch(
        f"/api/v1/projects/{project_id}/tasks/{second_task_id}/schedules/{schedule_id}",
        headers=headers,
        json={"enabled": False},
    )
    assert wrong_update.status_code == 404, wrong_update.text

    other_project = _create_project(client, headers, "SCHED-SCOPE-OTHER")
    other_task_id = _seed_task(client, headers, other_project, name="other")
    cross_project = client.get(
        f"/api/v1/projects/{other_project}/tasks/{other_task_id}/schedules",
        headers=headers,
    )
    assert cross_project.status_code == 200
    assert cross_project.json() == []

    wrong_project_task = client.get(
        f"/api/v1/projects/{other_project}/tasks/{first_task_id}/schedules",
        headers=headers,
    )
    assert wrong_project_task.status_code == 404

    wrong_delete = client.delete(
        f"/api/v1/projects/{project_id}/tasks/{second_task_id}/schedules/{schedule_id}",
        headers=headers,
    )
    assert wrong_delete.status_code == 404, wrong_delete.text


def test_schedule_update_rejects_invalid_interval(client):
    headers = _create_admin(client, username="sched-update-admin")
    project_id = _create_project(client, headers, "SCHED-UPDATE")
    task_id = _seed_task(client, headers, project_id)
    created = client.post(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules",
        headers=headers,
        json={"interval_seconds": 60},
    )
    assert created.status_code == 201

    updated = client.patch(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules/{created.json()['schedule_id']}",
        headers=headers,
        json={"interval_seconds": 0},
    )
    assert updated.status_code == 422

    too_long = client.patch(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules/{created.json()['schedule_id']}",
        headers=headers,
        json={"interval_seconds": 365 * 24 * 3600 + 1},
    )
    assert too_long.status_code == 422

    invalid_cron = client.patch(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules/{created.json()['schedule_id']}",
        headers=headers,
        json={"cron_expression": "not-a-cron"},
    )
    assert invalid_cron.status_code == 422


def test_schedule_create_rejects_boolean_interval(client):
    """布尔值不能借助 Pydantic 的 int 转换绕过 interval 校验。"""
    headers = _create_admin(client, username="sched-bool-admin")
    project_id = _create_project(client, headers, "SCHED-BOOL")
    task_id = _seed_task(client, headers, project_id)

    resp = client.post(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules",
        headers=headers,
        json={"interval_seconds": True},
    )
    assert resp.status_code == 422
