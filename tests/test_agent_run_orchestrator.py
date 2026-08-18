"""P6.4：Agent 执行编排器（RunOrchestrator）测试。

验证编排器把执行器、任务上下文与插件执行面串成完整闭环：
1. 插件执行成功后产生 run.result 到 outbox（稳定 ID）
2. 插件日志经 collect_logs + flush 以 run.log 批量入 outbox
3. 插件 analyze_results 的结构化 case 结果随 run.result 上报
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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


def _make(tmp_path):
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentPluginRegistry()
    registry.register_installed(_ExecPlugin())
    execution = ExecutionService(_SETTINGS, ledger)
    orchestrator = RunOrchestrator(
        _SETTINGS,
        ledger,
        execution,
        registry,
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
