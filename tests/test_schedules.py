"""P8.2：任务调度计划 API 测试。

验收要点（D-18，§18.7）：
1. cron 与 interval 互斥校验
2. 调度计划 CRUD（项目 owner 创建、列表、更新、删除）
3. 无效 cron 表达式拒绝
4. viewer 不能创建调度计划
"""

from __future__ import annotations

from sqlalchemy import text


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
    with container.uow_factory()() as uow:
        from master.domain.models import TestScript, TestTask
        from master.domain.enums import ScriptParseStatus, ScriptParseLocation

        uow.test_scripts.add(
            TestScript(
                project_id=project_id,
                script_id="S-DUMMY",
                task_type="pytest",
                name="dummy",
                version=1,
                file_ref="data/scripts/S-DUMMY/1/dummy.py",
                size=10,
                sha256="a" * 64,
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
                task_id="T-SCHED",
                project_id=project_id,
                script_id="S-DUMMY",
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
    assert "cron_expression" in resp.json()["detail"]

    # 两个都提供
    resp = client.post(
        f"/api/v1/projects/{project_id}/tasks/{task_id}/schedules",
        headers=headers,
        json={"cron_expression": "*/5 * * * *", "interval_seconds": 60},
    )
    assert resp.status_code == 422
    assert "互斥" in resp.json()["detail"]


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
    assert "cron" in resp.json()["detail"].lower()


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
