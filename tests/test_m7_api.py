"""M7-3： 认证、项目、任务和 Run 查询 API 测试。"""

from __future__ import annotations

from aetp_protocol.ids import BusinessId
from aetp_protocol.task import TestTask as ProtocolTestTask

from master.domain.enums import CaseStatus
from master.domain.models import RunCaseResult
from master.domain.time import utcnow
from tests.test_task_service import SCRIPT_A, _binding, _definition


def _login_v2(client) -> dict[str, str]:
    auth = client.app.state.container.auth_service()
    auth.bootstrap_admin("m7-v2-admin", "admin-pass-123", "M7  Admin")
    response = client.post(
        "/api/v2/auth/login",
        json={"username": "m7-v2-admin", "password": "admin-pass-123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_api_supports_project_task_run_queries(client) -> None:
    headers = _login_v2(client)
    project_response = client.post(
        "/api/v2/projects",
        headers=headers,
        json={"project_key": "M7", "name": "M7  Project"},
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["project_id"]

    assert client.get("/api/v2/auth/me", headers=headers).status_code == 200
    projects = client.get("/api/v2/projects", headers=headers)
    assert projects.status_code == 200
    assert any(item["project_id"] == project_id for item in projects.json())

    definition = _definition(SCRIPT_A, name="m7-script").model_copy(
        update={"project_id": BusinessId(project_id)}
    )
    definition_response = client.post(
        f"/api/v2/projects/{project_id}/script-definitions",
        headers=headers,
        json={"definition": definition.model_dump(mode="json")},
    )
    assert definition_response.status_code == 201, definition_response.text

    task = ProtocolTestTask(
        task_id=BusinessId("01J00000000000000000000049"),
        project_id=BusinessId(project_id),
        revision=1,
        name="m7-task",
        scripts=(_binding("01J0000000000000000000004A", definition, 0),),
    )
    task_response = client.post(
        f"/api/v2/projects/{project_id}/test-tasks",
        headers=headers,
        json={"task": task.model_dump(mode="json")},
    )
    assert task_response.status_code == 201, task_response.text

    definitions = client.get(
        f"/api/v2/projects/{project_id}/script-definitions",
        headers=headers,
    )
    assert definitions.status_code == 200
    assert definitions.json()[0]["script_definition_id"] == definition.script_definition_id.root

    tasks = client.get(f"/api/v2/projects/{project_id}/test-tasks", headers=headers)
    assert tasks.status_code == 200
    assert tasks.json()[0]["task"]["task_id"] == task.task_id.root

    idempotent_headers = {**headers, "Idempotency-Key": "m7-v2-run-create-001"}
    run_response = client.post(
        f"/api/v2/projects/{project_id}/runs",
        headers=idempotent_headers,
        json={"task_id": task.task_id.root},
    )
    assert run_response.status_code == 201, run_response.text
    run_id = run_response.json()["run_id"]
    replay_response = client.post(
        f"/api/v2/projects/{project_id}/runs",
        headers=idempotent_headers,
        json={"task_id": task.task_id.root},
    )
    assert replay_response.status_code == 201, replay_response.text
    assert replay_response.json()["run_id"] == run_id

    runs = client.get(f"/api/v2/projects/{project_id}/runs", headers=headers)
    assert runs.status_code == 200
    assert any(item["run_id"] == run_id for item in runs.json())

    detail = client.get(
        f"/api/v2/projects/{project_id}/runs/{run_id}",
        headers=headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["snapshot"]["task_id"] == task.task_id.root
    assert len(detail.json()["shards"]) == 2

    first_shard = detail.json()["shards"][0]
    with client.app.state.container.uow_factory()() as uow:
        uow.run_case_results.add_many(
            [
                RunCaseResult(
                    run_id=run_id,
                    shard_id=first_shard["shard_id"],
                    case_key=first_shard["case_keys"][0],
                    status=CaseStatus.FAILED,
                    error_summary="expected test failure",
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            ]
        )
        uow.run_case_results.add_many(
            [
                RunCaseResult(
                    run_id=run_id,
                    shard_id=detail.json()["shards"][1]["shard_id"],
                    case_key=detail.json()["shards"][1]["case_keys"][0],
                    attempt_no=1,
                    status=CaseStatus.FAILED,
                    error_summary="historical failure",
                    created_at=utcnow(),
                    updated_at=utcnow(),
                ),
                RunCaseResult(
                    run_id=run_id,
                    shard_id=detail.json()["shards"][1]["shard_id"],
                    case_key=detail.json()["shards"][1]["case_keys"][0],
                    attempt_no=2,
                    status=CaseStatus.PASSED,
                    created_at=utcnow(),
                    updated_at=utcnow(),
                ),
            ]
        )
    retry_headers = {**headers, "Idempotency-Key": "m7-v2-retry-failed-001"}
    retry_failed = client.post(
        f"/api/v2/projects/{project_id}/runs/{run_id}/retry-failed",
        headers=retry_headers,
        json={},
    )
    assert retry_failed.status_code == 201, retry_failed.text
    assert retry_failed.json()["snapshot"]["scripts"][0]["selected_case_keys"] == [first_shard["case_keys"][0]]
    retry_replay = client.post(
        f"/api/v2/projects/{project_id}/runs/{run_id}/retry-failed",
        headers=retry_headers,
        json={},
    )
    assert retry_replay.status_code == 201, retry_replay.text
    assert retry_replay.json()["run_id"] == retry_failed.json()["run_id"]
