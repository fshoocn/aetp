"""P6.4：Agent 执行编排器（RunOrchestrator）测试。

验证编排器把执行器、任务上下文与插件执行面串成完整闭环：
1. 插件执行成功后产生 run.result 到 outbox（稳定 ID）
2. 插件日志经 collect_logs + flush 以 run.log 批量入 outbox
3. 插件 analyze_results 的结构化 case 结果随 run.result 上报
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aetp_protocol.envelope import Envelope
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import RunAssignPayload, RunResultPayload

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.execution_service import ExecutionService
from agent.application.services.run_orchestrator import RunOrchestrator
from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus
from agent.plugins import AgentPluginRegistry


def _now() -> datetime:
    return datetime(2099, 1, 1, tzinfo=timezone.utc)


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
    max_concurrent_runs=2,
)


class _ExecPlugin:
    """执行面：execute 写日志，analyze 返回结构化 case 结果。"""

    task_type = "e2e_agent"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = "E2E Agent"
    verify_location = "master"
    parse_location = "master"

    def verify_script(self, script_dir: str, config) -> list[str]:
        return []

    def parse_cases(self, script_dir: str, config) -> list:
        return []

    async def execute(self, context):
        await context.log("info", "starting")
        await context.progress(50, "running", "half")
        return {"status": "passed", "total": 2}

    async def cancel(self):
        return None

    async def analyze_results(self, execution_result, context):
        return {
            "passed": True,
            "case_results": [
                {"case_key": "c0", "status": "passed", "duration_ms": 10},
                {"case_key": "c1", "status": "passed", "duration_ms": 20},
            ],
            "metrics": {"total": 2},
        }

    async def collect_logs(self, context):
        await context.log("info", "collected")


class _FailingExecPlugin(_ExecPlugin):
    async def execute(self, context):
        raise RuntimeError("execute boom")

    async def analyze_results(self, execution_result, context):
        raise AssertionError("执行失败时不应分析结果")


class _FailedResultPlugin(_ExecPlugin):
    async def execute(self, context):
        return {"return_code": 1}

    async def analyze_results(self, execution_result, context):
        return {
            "passed": False,
            "case_results": [
                {"case_key": "c0", "status": "failed", "duration_ms": 10}
            ],
        }


class _ArtifactPlugin(_ExecPlugin):
    async def execute(self, context):
        report = Path(context.script_ref["path"]) / "report.xml"
        report.write_text("<testsuite />", encoding="utf-8")
        return {"report_path": str(report), "return_code": 0}


class _FakeArtifactUploader:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def upload(self, url, path, *, kind, filename=None):
        self.calls.append(
            {"url": url, "path": str(path), "kind": kind, "filename": filename}
        )
        return {"artifact_id": "A-1", "kind": kind, "filename": filename}


def _payload(**kw) -> RunAssignPayload:
    base: dict[str, Any] = dict(
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        shard_index=0,
        run_id="R-1",
        attempt_no=1,
        dispatch_id="D-1",
        task_type="e2e_agent",
        plugin_version="1.0.0",
        script_ref={"script_id": "S-1", "version": 1, "sha256": "a" * 64},
        case_keys=["c0", "c1"],
        timeout_s=30,
    )
    base.update(kw)
    return RunAssignPayload(**base)


def _make(tmp_path, plugin=None, artifact_uploader=None):
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentPluginRegistry()
    registry.register_installed(plugin or _ExecPlugin())
    execution = ExecutionService(_SETTINGS, ledger)
    orchestrator = RunOrchestrator(
        _SETTINGS,
        ledger,
        execution,
        registry,
        artifact_uploader=artifact_uploader,
        session_id=lambda: "sess-1",
        now=_now,
    )
    return ledger, orchestrator


def _claim_outbox(ledger) -> list[Envelope]:
    pending = ledger.claim_due_outbox(100, _now().replace(tzinfo=None))
    return [Envelope.model_validate(e.payload) for e in pending]


@pytest.mark.asyncio
async def test_orchestrator_executes_and_reports_result(tmp_path) -> None:
    ledger, orchestrator = _make(tmp_path)
    ledger.claim_run("R-1", 1)

    await orchestrator._run(_payload())

    run = ledger.get_run("R-1")
    assert run is not None
    assert run.status is AgentRunStatus.SUCCEEDED

    envelopes = _claim_outbox(ledger)
    results = [
        e for e in envelopes if e.message_type == MessageType.RUN_RESULT.value
    ]
    assert len(results) == 1
    result = RunResultPayload.model_validate(results[0].payload)
    assert result.run_id == "R-1"
    assert result.shard_id == "SH-1"
    assert result.attempt_no == 1
    assert result.status == "succeeded"
    assert result.passed is True
    assert [c.case_key for c in result.case_results] == ["c0", "c1"]


@pytest.mark.asyncio
async def test_orchestrator_does_not_analyze_failed_execution(tmp_path) -> None:
    ledger, orchestrator = _make(tmp_path, _FailingExecPlugin())
    ledger.claim_run("R-1", 1)

    await orchestrator._run(_payload())

    run = ledger.get_run("R-1")
    assert run is not None
    assert run.status is AgentRunStatus.FAILED

    envelopes = _claim_outbox(ledger)
    result = RunResultPayload.model_validate(
        next(e.payload for e in envelopes if e.message_type == MessageType.RUN_RESULT.value)
    )
    assert result.status == "failed"
    assert result.data["error"] == "RuntimeError: execute boom"


@pytest.mark.asyncio
async def test_orchestrator_maps_failed_analysis_to_failed_status(tmp_path) -> None:
    ledger, orchestrator = _make(tmp_path, _FailedResultPlugin())
    ledger.claim_run("R-1", 1)

    await orchestrator._run(_payload())

    envelopes = _claim_outbox(ledger)
    result = RunResultPayload.model_validate(
        next(e.payload for e in envelopes if e.message_type == MessageType.RUN_RESULT.value)
    )
    assert result.status == "failed"
    assert result.passed is False
    assert result.case_results[0].status == "failed"


@pytest.mark.asyncio
async def test_orchestrator_uploads_report_and_reports_artifact_refs(tmp_path) -> None:
    uploader = _FakeArtifactUploader()
    ledger, orchestrator = _make(tmp_path, _ArtifactPlugin(), uploader)
    ledger.claim_run("R-1", 1)

    await orchestrator._run(
        _payload(
            script_ref={"path": str(tmp_path)},
            artifact_upload_url="http://master.local/artifacts",
        )
    )

    assert len(uploader.calls) == 1
    assert uploader.calls[0]["kind"] == "report"
    envelopes = _claim_outbox(ledger)
    result = RunResultPayload.model_validate(
        next(e.payload for e in envelopes if e.message_type == MessageType.RUN_RESULT.value)
    )
    complete = next(
        e.payload for e in envelopes if e.message_type == MessageType.RUN_LOG_COMPLETE.value
    )
    assert result.artifact_refs == [{"artifact_id": "A-1", "kind": "report", "filename": "pytest-junit.xml"}]
    assert complete["artifact_refs"] == result.artifact_refs


@pytest.mark.asyncio
async def test_orchestrator_flushes_logs_to_outbox(tmp_path) -> None:
    ledger, orchestrator = _make(tmp_path)
    ledger.claim_run("R-1", 1)

    await orchestrator._run(_payload())

    envelopes = _claim_outbox(ledger)
    logs = [e for e in envelopes if e.message_type == MessageType.RUN_LOG.value]
    assert len(logs) == 1
    batch = logs[0].payload
    assert batch["run_id"] == "R-1"
    assert [entry["message"] for entry in batch["entries"]] == [
        "starting",
        "collected",
    ]


@pytest.mark.asyncio
async def test_orchestrator_result_outbox_id_stable(tmp_path) -> None:
    """一个 attempt 只产生一个 result（稳定 outbox ID 幂等）。"""
    ledger, orchestrator = _make(tmp_path)
    ledger.claim_run("R-1", 1)

    await orchestrator._run(_payload())
    # 再跑一次（同 attempt 重放），replace_outbox 幂等，仍是单条 result
    await orchestrator._run(_payload())

    envelopes = _claim_outbox(ledger)
    results = [
        e for e in envelopes if e.message_type == MessageType.RUN_RESULT.value
    ]
    assert len(results) == 1


@pytest.mark.asyncio
async def test_orchestrator_unpacks_zip_script_to_dir(tmp_path) -> None:
    """脚本包是 zip 时，_enrich_script_ref 解包成目录并注入 path（§9.8）。"""
    import hashlib
    import io
    import zipfile

    from agent.application.services.script_cache_service import ScriptCacheService

    # 构造 zip 脚本包（含 test_sample.py）
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("test_sample.py", "def test_a():\n    pass\n")
    data = buffer.getvalue()
    sha256 = hashlib.sha256(data).hexdigest()

    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    cache = ScriptCacheService(
        tmp_path / "cache",
        ledger,
        fetcher=lambda url: data,
    )
    registry = AgentPluginRegistry()
    registry.register_installed(_ExecPlugin())
    execution = ExecutionService(_SETTINGS, ledger)
    orchestrator = RunOrchestrator(
        _SETTINGS,
        ledger,
        execution,
        registry,
        script_cache=cache,
        session_id=lambda: "sess-1",
        now=_now,
    )

    script_ref = {
        "script_id": "S-1",
        "version": 1,
        "sha256": sha256,
        "download_url": "http://master.local/internal/scripts/S-1/download",
    }
    enriched = orchestrator._enrich_script_ref(dict(script_ref))
    path = enriched.get("path")
    assert path, "应注入解包目录路径"
    script_dir = Path(path)
    assert script_dir.is_dir()
    assert (script_dir / "test_sample.py").is_file(), "zip 应被解压出 test 文件"

    # 清理后目录被移除
    orchestrator._cleanup_script_dirs()
    assert not script_dir.exists()


@pytest.mark.asyncio
async def test_orchestrator_materializes_single_file_as_python(tmp_path) -> None:
    import hashlib

    from agent.application.services.script_cache_service import ScriptCacheService

    data = b"def test_a():\n    pass\n"
    sha256 = hashlib.sha256(data).hexdigest()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    cache = ScriptCacheService(
        tmp_path / "cache",
        ledger,
        fetcher=lambda url: data,
    )
    registry = AgentPluginRegistry()
    registry.register_installed(_ExecPlugin())
    execution = ExecutionService(_SETTINGS, ledger)
    orchestrator = RunOrchestrator(
        _SETTINGS,
        ledger,
        execution,
        registry,
        script_cache=cache,
        session_id=lambda: "sess-1",
        now=_now,
    )

    enriched = orchestrator._enrich_script_ref(
        {
            "script_id": "S-1",
            "version": 1,
            "sha256": sha256,
            "download_url": "http://master.local/internal/scripts/S-1/download",
        }
    )
    script_dir = Path(enriched["path"])
    assert (script_dir / "test_script.py").is_file()
    assert not (script_dir / "S-1-v1.bin").exists()
    orchestrator._cleanup_script_dirs()
