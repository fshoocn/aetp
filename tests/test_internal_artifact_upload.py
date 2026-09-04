"""Master 内部 Run Artifact 上传端点（Agent → Master multipart）测试。"""

from __future__ import annotations

from tests.test_task_service import (
    PROJECT_ID,
    SCRIPT_A,
    _binding,
    _definition,
    _seed_project,
)

PLUGIN = "org.pytest.executor"


def _seed_run(client):
    """建项目 + 脚本 + 任务 + Run，返回 (container, run_id, shard_id)。"""
    from aetp_protocol.ids import BusinessId
    from aetp_protocol.task import TestTask as ProtocolTestTask

    container = client.app.state.container
    _seed_project(container)

    service = container.task_service()
    definition = service.register_script_definition(_definition(SCRIPT_A, name="alpha"))
    task_id = BusinessId("01J00000000000000000000090")
    task = ProtocolTestTask(
        task_id=task_id,
        project_id=PROJECT_ID,
        revision=1,
        name="artifact task",
        scripts=(_binding("01J00000000000000000000091", definition.definition, 0),),
    )
    service.create_task(task, created_by=definition.id or 1)
    created = service.create_run(task_id, project_id=PROJECT_ID)
    run_id = created.run.run_id
    shard_id = created.shards[0].shard_id
    return container, run_id, shard_id


def test_agent_artifact_upload_endpoint_returns_artifact(client) -> None:
    """Agent 用签名 URL POST multipart 文件 → Master 登记产物并返回 artifact_id。"""
    container, run_id, shard_id = _seed_run(client)
    node_id = "01J00000000000000000000092"

    # 构造 Master 侧签名上传 URL（与 scheduler 为 Plan 生成的同源）
    url = container.artifact_upload_signing_service().build_url(
        run_id,
        PROJECT_ID.root,
        node_id,
        shard_id,
    )
    assert url.startswith(f"/api/v2/internal/runs/{run_id}/artifacts")

    response = client.post(
        url,
        files={"file": ("pytest-junit.xml", b"<testsuite />", "application/xml")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["artifact_id"]
    assert body["run_id"] == run_id
    assert body["kind"] == "report"
    assert body["filename"] == "pytest-junit.xml"
    assert body["sha256"]

    # 产物已登记到 DB（项目范围可见）
    with container.uow_factory()() as uow:
        artifacts = uow.run_artifacts.list_by_run(run_id)
        assert any(item.artifact_id == body["artifact_id"] for item in artifacts)


def test_agent_artifact_upload_rejects_bad_signature(client) -> None:
    """签名错误 → 403（此前因端点缺失会 405 落到 SPA）。"""
    container, run_id, shard_id = _seed_run(client)
    node_id = "01J00000000000000000000093"
    bad_url = (
        f"/api/v2/internal/runs/{run_id}/artifacts"
        f"?project_id={PROJECT_ID.root}&node_id={node_id}&shard_id={shard_id}"
        f"&expires=9999999999&signature=deadbeef"
    )
    response = client.post(
        bad_url,
        files={"file": ("x.xml", b"<x/>", "application/xml")},
    )
    assert response.status_code == 403, response.text
