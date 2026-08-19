"""P6.6：结束产物上传（run_artifacts）+ run.log-complete 日志围栏测试。

验收要点（§15.3 P6.6：围栏后日志被拒；前端停止轮询）：
1. handle_log_complete 置位 log_complete + last_log_sequence，触发 run.log_complete
2. 围栏后 handle_log 拒绝任何日志条目
3. 围栏幂等（重复 log-complete 不重复广播）
4. 产物登记：写 run_artifacts 引用 + 文件落盘 + sha256
5. 内部上传端点 + 项目范围产物列表/下载
6. Agent 编排器 flush 后发布 run.log-complete
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from aetp_protocol.logs import RunLogBatch
from aetp_protocol.payloads import RunLogCompletePayload

from master.domain.models import RunArtifact
from master.domain.time import utcnow


def _uow(container):
    return container.uow_factory()()


def _log_entry(run_id: str, sequence: int, message: str) -> dict:
    return {
        "project_id": "p1",
        "task_id": "T-e2e",
        "run_id": run_id,
        "node_id": "node-a",
        "sequence": sequence,
        "level": "info",
        "message": message,
        "occurred_at": "2026-08-18T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# 日志围栏
# ---------------------------------------------------------------------------


def test_log_complete_fences_log(client) -> None:
    container = client.app.state.container
    run_id = _seed_run(container)

    service = container.run_projection_service()

    # 围栏前日志可落库
    service.handle_log("node-a", RunLogBatch(
        run_id=run_id, first_sequence=1, entries=[_log_entry(run_id, 1, "hello")]
    ))

    # 发布围栏
    pr = service.handle_log_complete(
        "node-a",
        RunLogCompletePayload(run_id=run_id, last_sequence=1, entry_count=1),
    )
    assert pr.handled is True
    assert pr.event_type == "run.log_complete"

    with _uow(container) as uow:
        run = uow.task_runs.get_by_run_id(run_id)
        assert run.log_complete is True
        assert run.last_log_sequence == 1

    # 围栏后日志被拒
    rejected = service.handle_log(
        "node-a",
        RunLogBatch(run_id=run_id, first_sequence=2, entries=[_log_entry(run_id, 2, "late")]),
    )
    assert rejected.handled is False

    with _uow(container) as uow:
        logs = uow.run_logs.list_by_run(run_id)
        assert [l.sequence for l in logs] == [1]


def test_log_complete_idempotent(client) -> None:
    container = client.app.state.container
    run_id = _seed_run(container)
    service = container.run_projection_service()

    first = service.handle_log_complete(
        "node-a", RunLogCompletePayload(run_id=run_id, last_sequence=0, entry_count=0)
    )
    second = service.handle_log_complete(
        "node-a", RunLogCompletePayload(run_id=run_id, last_sequence=0, entry_count=0)
    )
    assert first.handled is True
    assert second.handled is False


def test_log_complete_accepts_late_logs_within_declared_range(client) -> None:
    container = client.app.state.container
    run_id = _seed_run(container)
    service = container.run_projection_service()

    service.handle_log_complete(
        "node-a",
        RunLogCompletePayload(run_id=run_id, last_sequence=2, entry_count=2),
    )

    late = service.handle_log(
        "node-a",
        RunLogBatch(
            run_id=run_id,
            first_sequence=1,
            entries=[_log_entry(run_id, 1, "late but declared")],
        ),
    )
    assert late.handled is True

    out_of_range = service.handle_log(
        "node-a",
        RunLogBatch(
            run_id=run_id,
            first_sequence=3,
            entries=[_log_entry(run_id, 3, "too late")],
        ),
    )
    assert out_of_range.handled is False

    with _uow(container) as uow:
        logs = uow.run_logs.list_by_run(run_id)
        assert [log.sequence for log in logs] == [1]


# ---------------------------------------------------------------------------
# 产物登记
# ---------------------------------------------------------------------------


def test_register_artifact_writes_ref_and_file(client) -> None:
    container = client.app.state.container
    run_id = _seed_run(container)
    service = container.artifact_service()

    artifact = service.register_artifact(
        run_id=run_id,
        project_id="p1",
        node_id="node-a",
        kind="report",
        filename="report.xml",
        data=b"<report>ok</report>",
    )

    assert artifact.artifact_id.startswith("A-")
    assert artifact.kind.value == "report"
    assert artifact.size == len(b"<report>ok</report>")
    assert artifact.sha256

    with _uow(container) as uow:
        stored = uow.run_artifacts.get_by_artifact_id(artifact.artifact_id)
        assert stored is not None
        assert stored.run_id == run_id
        assert stored.file_ref == f"artifacts/{run_id}/report.xml"


def test_register_artifact_rejects_unknown_run(client) -> None:
    container = client.app.state.container
    from master.application.errors import RunNotFoundError

    with pytest.raises(RunNotFoundError):
        container.artifact_service().register_artifact(
            run_id="missing",
            project_id="p1",
            node_id="node-a",
            kind="report",
            filename="r.xml",
            data=b"x",
        )


def test_list_and_get_artifact_project_scoped(client) -> None:
    container = client.app.state.container
    run_id = _seed_run(container)
    service = container.artifact_service()
    service.register_artifact(
        run_id=run_id, project_id="p1", node_id="node-a",
        kind="log_archive", filename="logs.jsonl", data=b'{"a":1}',
    )

    artifacts = service.list_by_run(run_id, "p1")
    assert len(artifacts) == 1
    assert artifacts[0].kind.value == "log_archive"

    # 跨项目不可见
    from master.application.errors import RunNotFoundError

    with pytest.raises(RunNotFoundError):
        service.list_by_run(run_id, "other-project")
    assert service.get_by_artifact_id(artifacts[0].artifact_id, "other-project") is None


# ---------------------------------------------------------------------------
# Agent 编排器：flush 后发布 run.log-complete
# ---------------------------------------------------------------------------


def test_orchestrator_publishes_log_complete(tmp_path) -> None:
    from agent.adapters.sqlite.ledger import SQLiteLedger
    from agent.application.services.execution_service import ExecutionService
    from agent.application.services.run_orchestrator import RunOrchestrator
    from agent.config import AgentSettings
    from agent.plugins import AgentPluginRegistry
    from aetp_protocol.message_types import MessageType
    from aetp_protocol.payloads import RunAssignPayload
    from aetp_protocol.envelope import Envelope

    class _Plugin:
        task_type = "t"
        plugin_version = "1.0.0"
        supported_versions = frozenset({"1.0.0"})
        display_name = ""
        verify_location = "master"
        parse_location = "master"

        async def execute(self, context):
            await context.log("info", "hello")
            return {"status": "passed"}

        async def cancel(self):
            return None

        async def analyze_results(self, execution_result, context):
            return {"passed": True, "case_results": []}

        async def collect_logs(self, context):
            return None

        def verify_script(self, script_dir, config):
            return []

        def parse_cases(self, script_dir, config):
            return []

    settings = AgentSettings(
        node_id="bench-001", name="bench", master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-001", mqtt_use_tls=False,
        max_concurrent_runs=1,
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentPluginRegistry()
    registry.register_installed(_Plugin())
    orchestrator = RunOrchestrator(
        settings, ledger, ExecutionService(settings, ledger), registry,
        session_id=lambda: "s", now=lambda: datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    payload = RunAssignPayload(
        project_id="p1", task_id="T-1", shard_id="SH-1", shard_index=0,
        run_id="R-1", attempt_no=1, dispatch_id="D-1", task_type="t",
        plugin_version="1.0.0",
        script_ref={"script_id": "S-1", "version": 1, "sha256": "a" * 64},
        case_keys=["c0"], timeout_s=30,
    )
    ledger.claim_run("R-1", 1)
    asyncio.run(orchestrator._run(payload))

    pending = ledger.claim_due_outbox(100, datetime(2099, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None))
    envelopes = [Envelope.model_validate(e.payload) for e in pending]
    completes = [e for e in envelopes if e.message_type == MessageType.RUN_LOG_COMPLETE.value]
    assert len(completes) == 1
    complete = RunLogCompletePayload.model_validate(completes[0].payload)
    assert complete.run_id == "R-1"
    assert complete.last_sequence == 1
    assert complete.entry_count == 1


# ---------------------------------------------------------------------------
# 种子
# ---------------------------------------------------------------------------


def _seed_run(container) -> str:
    """创建最小 Run（无分片/节点依赖），供围栏与产物测试。"""
    from master.domain.enums import (
        AccountStatus, PlatformRole, ProjectStatus, RunStatus,
        ScriptParseLocation, ScriptParseStatus, TriggerType,
    )
    from master.domain.models import (
        Project, TaskRun, TestScript, TestTask, User,
    )
    from aetp_protocol.capabilities import HardwareRequirements

    with _uow(container) as uow:
        user = uow.users.add(
            User(id=None, username="p66_owner", password_hash="h", display_name="",
                 account_status=AccountStatus.ACTIVE, platform_role=PlatformRole.USER,
                 created_at=utcnow(), updated_at=utcnow())
        )
        uow.projects.add(
            Project(id=None, project_id="p1", project_key="P1", name="P", description="",
                    status=ProjectStatus.ACTIVE, created_by=user.id,
                    created_at=utcnow(), updated_at=utcnow())
        )
    with _uow(container) as uow:
        script = uow.test_scripts.add(
            TestScript(id=None, project_id="p1", script_id="S-p66", task_type="t",
                       name="p66", version=1, file_ref="data/scripts/S-p66/1", size=1,
                       sha256="a" * 64, config={},
                       hardware_requirements=HardwareRequirements(),
                       parse_status=ScriptParseStatus.PARSED,
                       parse_location=ScriptParseLocation.MASTER,
                       result_parse_location=ScriptParseLocation.MASTER,
                       plugin_version="1.0.0", created_by=user.id)
        )
        task = uow.test_tasks.add(
            TestTask(project_id="p1", task_id="T-p66", script_id=script.script_id,
                     script_version=1, task_type="t", name="p66-task",
                     default_case_selection=["c0"], node_ids=[],
                     split_policy={"type": "none"}, retry_policy={},
                     timeout_s=30, enabled=True, created_by=user.id)
        )
        run = uow.task_runs.add(
            TaskRun(run_id="R-p66", project_id="p1", task_id=task.task_id,
                    script_ref={"script_id": "S-p66", "version": 1, "sha256": "a" * 64},
                    case_selection=["c0"], split_policy={"type": "none"},
                    trigger_type=TriggerType.MANUAL_WEB, triggered_by_user_id=user.id,
                    status=RunStatus.CREATED)
        )
    return run.run_id
