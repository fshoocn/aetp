""" ScriptDefinition、TestTask 和 Run API 集成测试。"""

from __future__ import annotations

from aetp_protocol.task import TestTask as ProtocolTestTask

from tests.test_task_service import (
    PROJECT_ID,
    SCRIPT_A,
    _binding,
    _definition,
)


def _seed_project(client) -> dict[str, str]:
    container = client.app.state.container
    auth = container.auth_service()
    assert auth.bootstrap_admin("v2-api-admin", "admin-pass-123", " API Admin")
    login = client.post(
        "/api/v2/auth/login",
        json={"username": "v2-api-admin", "password": "admin-pass-123"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    with container.uow_factory()() as uow:
        user = uow.users.get_by_username("v2-api-admin")
        assert user is not None and user.id is not None
        from master.domain.enums import ProjectStatus
        from master.domain.models import Project
        from master.domain.time import utcnow

        now = utcnow()
        uow.projects.add(
            Project(
                id=None,
                project_id=PROJECT_ID.root,
                project_key="API",
                name=" API",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
        )
    return headers


def test_task_and_run_api_creates_snapshot_and_pending_shards(client) -> None:
    headers = _seed_project(client)
    definition = _definition(SCRIPT_A, name="alpha")
    response = client.post(
        f"/api/v2/projects/{PROJECT_ID.root}/script-definitions",
        headers=headers,
        json={"definition": definition.model_dump(mode="json")},
    )
    assert response.status_code == 201, response.text

    task = ProtocolTestTask(
        task_id="01J00000000000000000000049",
        project_id=PROJECT_ID,
        revision=1,
        name="api task",
        scripts=(_binding("01J0000000000000000000004A", definition, 0),),
    )
    response = client.post(
        f"/api/v2/projects/{PROJECT_ID.root}/test-tasks",
        headers=headers,
        json={"task": task.model_dump(mode="json")},
    )
    assert response.status_code == 201, response.text

    response = client.post(
        f"/api/v2/projects/{PROJECT_ID.root}/runs",
        headers=headers,
        json={"task_id": task.task_id.root},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["task_id"] == task.task_id.root
    assert body["snapshot"]["scripts"][0]["source"]["download_url"] is None
    assert len(body["shards"]) == 2
    assert body["scheduled"] == 0
    assert len(body["pending_shard_ids"]) == 2


def test_disable_endpoints_enforce_reference_preconditions(client) -> None:
    """删脚本前必须先删任务；任务有 Run 时也不能删。"""
    from aetp_protocol.ids import BusinessId

    headers = _seed_project(client)
    definition = _definition(SCRIPT_A, name="alpha")
    response = client.post(
        f"/api/v2/projects/{PROJECT_ID.root}/script-definitions",
        headers=headers,
        json={"definition": definition.model_dump(mode="json")},
    )
    assert response.status_code == 201, response.text

    task_id = BusinessId("01J0000000000000000000004B")
    task = ProtocolTestTask(
        task_id=task_id,
        project_id=PROJECT_ID,
        revision=1,
        name="disable api task",
        scripts=(_binding("01J0000000000000000000004C", definition, 0),),
    )
    response = client.post(
        f"/api/v2/projects/{PROJECT_ID.root}/test-tasks",
        headers=headers,
        json={"task": task.model_dump(mode="json")},
    )
    assert response.status_code == 201, response.text

    # 脚本仍被任务引用 → 409
    response = client.delete(
        f"/api/v2/projects/{PROJECT_ID.root}/script-definitions/{SCRIPT_A.root}?revision=1",
        headers=headers,
    )
    assert response.status_code == 409, response.text

    # 任务被删（先建一个 Run 让它无法删除）→ 409
    response = client.post(
        f"/api/v2/projects/{PROJECT_ID.root}/runs",
        headers=headers,
        json={"task_id": task_id.root},
    )
    assert response.status_code == 201, response.text
    response = client.delete(
        f"/api/v2/projects/{PROJECT_ID.root}/test-tasks/{task_id.root}",
        headers=headers,
    )
    assert response.status_code == 409, response.text

