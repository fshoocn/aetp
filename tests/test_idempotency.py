"""高风险 Run 写 API 的 Idempotency-Key 测试。"""

from __future__ import annotations

from tests.test_p6_4_end_to_end import _add_tester_as_member, _seed


def test_trigger_run_idempotency_replays_response(client, auth_header) -> None:
    container = client.app.state.container
    _user_id, task_id = _seed(container)
    _add_tester_as_member(container)
    headers = {**auth_header, "Idempotency-Key": "run-create-001"}

    first = client.post(
        "/api/v1/projects/p1/runs",
        json={"task_id": task_id},
        headers=headers,
    )
    second = client.post(
        "/api/v1/projects/p1/runs",
        json={"task_id": task_id},
        headers=headers,
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    with container.uow_factory()() as uow:
        assert len(uow.task_runs.list(project_id="p1")) == 1


def test_trigger_run_idempotency_rejects_different_payload(client, auth_header) -> None:
    container = client.app.state.container
    _user_id, task_id = _seed(container)
    _add_tester_as_member(container)
    headers = {**auth_header, "Idempotency-Key": "run-create-002"}

    first = client.post(
        "/api/v1/projects/p1/runs",
        json={"task_id": task_id},
        headers=headers,
    )
    conflict = client.post(
        "/api/v1/projects/p1/runs",
        json={"task_id": task_id, "case_filter": ["different"]},
        headers=headers,
    )

    assert first.status_code == 201, first.text
    assert conflict.status_code == 409, conflict.text
