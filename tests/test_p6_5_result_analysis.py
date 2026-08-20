"""P6.5：Agent 结果分析 + 结构化 case 结果上报测试。

验收要点（§15.3 P6.5：一个 attempt 只接收一个最终结果；CANoe 时序——
运行中无 case 级实时数据，结束后 analyze_results 分析报告产出）：
1. analyze_results 产出结构化 CaseResultEntry 随 run.result 上报
2. analyze_results 异常 → result 标记 failed（§9.8 保留原始报告，不误报 succeeded）
3. analyze_results 返回非 Mapping → failed
4. 无 analyze_results 方法 → 以执行态为结果（passed 取执行成功）
5. 协议层 CaseResultEntry 强类型校验（非法 status/duration 拒绝）
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    CaseResultEntry,
    RunAssignPayload,
    RunResultPayload,
)
from pydantic import ValidationError

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.execution_service import ExecutionService
from agent.application.services.run_orchestrator import RunOrchestrator
from agent.config import AgentSettings
from agent.plugins import AgentPluginRegistry


def _now() -> datetime:
    return datetime(2099, 1, 1, tzinfo=UTC)


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
    max_concurrent_runs=2,
)


def _payload(**kw) -> RunAssignPayload:
    base: dict[str, Any] = {
        "project_id": "p1",
        "task_id": "T-1",
        "shard_id": "SH-1",
        "shard_index": 0,
        "run_id": "R-1",
        "attempt_no": 1,
        "dispatch_id": "D-1",
        "task_type": "t",
        "plugin_version": "1.0.0",
        "script_ref": {"script_id": "S-1", "version": 1, "sha256": "a" * 64},
        "case_keys": ["c0", "c1"],
        "timeout_s": 30,
    }
    base.update(kw)
    return RunAssignPayload(**base)


def _make(tmp_path, plugin):
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentPluginRegistry()
    registry.register_installed(plugin)
    execution = ExecutionService(_SETTINGS, ledger)
    orchestrator = RunOrchestrator(
        _SETTINGS, ledger, execution, registry, session_id=lambda: "s", now=_now
    )
    return ledger, orchestrator


def _claim_result(ledger) -> RunResultPayload:
    pending = ledger.claim_due_outbox(100, _now().replace(tzinfo=None))
    for entry in pending:
        from aetp_protocol.envelope import Envelope

        env = Envelope.model_validate(entry.payload)
        if env.message_type == MessageType.RUN_RESULT.value:
            return RunResultPayload.model_validate(env.payload)
    raise AssertionError("未找到 run.result")


class _AnalyzePlugin:
    task_type = "t"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = ""

    def __init__(self, *, analyze=None) -> None:
        self._analyze_impl = analyze

    async def execute(self, context):
        return {"status": "passed"}

    async def cancel(self):
        return None

    async def analyze_results(self, execution_result, context):
        if self._analyze_impl is None:
            return {"passed": True, "case_results": []}
        result = self._analyze_impl(execution_result, context)
        if asyncio.iscoroutine(result):
            result = await result
        return result


class _NoAnalyzePlugin:
    task_type = "t"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = ""

    async def execute(self, context):
        return {"status": "passed"}

    async def cancel(self):
        return None


@pytest.mark.asyncio
async def test_analyze_produces_typed_case_results(tmp_path) -> None:
    async def analyze(execution_result, context):
        return {
            "passed": False,
            "case_results": [
                {"case_key": "c0", "status": "passed", "duration_ms": 10},
                {"case_key": "c1", "status": "failed", "error_summary": "assert"},
            ],
            "metrics": {"total": 2},
        }

    ledger, orchestrator = _make(tmp_path, _AnalyzePlugin(analyze=analyze))
    ledger.claim_run("R-1", 1)
    await orchestrator._run(_payload())

    result = _claim_result(ledger)
    assert result.passed is False
    assert [(c.case_key, c.status) for c in result.case_results] == [
        ("c0", "passed"),
        ("c1", "failed"),
    ]
    assert result.metrics == {"total": 2}


@pytest.mark.asyncio
async def test_analyze_exception_marks_failed(tmp_path) -> None:
    """§9.8：分析失败 → result 标记 failed，不误报 succeeded。"""

    async def analyze(execution_result, context):
        raise RuntimeError("report parse failed")

    ledger, orchestrator = _make(tmp_path, _AnalyzePlugin(analyze=analyze))
    ledger.claim_run("R-1", 1)
    await orchestrator._run(_payload())

    result = _claim_result(ledger)
    assert result.status == "failed"
    assert result.passed is False
    assert "report parse failed" in result.data["error"]


@pytest.mark.asyncio
async def test_analyze_non_mapping_marks_failed(tmp_path) -> None:
    async def analyze(execution_result, context):
        return "not-a-mapping"

    ledger, orchestrator = _make(tmp_path, _AnalyzePlugin(analyze=analyze))
    ledger.claim_run("R-1", 1)
    await orchestrator._run(_payload())

    result = _claim_result(ledger)
    assert result.status == "failed"
    assert "Mapping" in result.data["error"]


@pytest.mark.asyncio
async def test_analyze_illegal_case_entry_marks_failed(tmp_path) -> None:
    """结构化 case 结果非法（状态不在白名单）→ failed。"""

    async def analyze(execution_result, context):
        return {"passed": True, "case_results": [{"case_key": "c0", "status": "bogus"}]}

    ledger, orchestrator = _make(tmp_path, _AnalyzePlugin(analyze=analyze))
    ledger.claim_run("R-1", 1)
    await orchestrator._run(_payload())

    result = _claim_result(ledger)
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_no_analyze_uses_execution_status(tmp_path) -> None:
    """无 analyze_results 方法 → 以执行态为结果。"""
    ledger, orchestrator = _make(tmp_path, _NoAnalyzePlugin())
    ledger.claim_run("R-1", 1)
    await orchestrator._run(_payload())

    result = _claim_result(ledger)
    assert result.status == "succeeded"
    assert result.passed is True
    assert result.case_results == []


# ---------------------------------------------------------------------------
# 协议层：CaseResultEntry 强类型校验
# ---------------------------------------------------------------------------


def test_case_result_entry_validates() -> None:
    entry = CaseResultEntry(case_key="c0", status="passed", duration_ms=10)
    assert entry.case_key == "c0"
    assert entry.duration_ms == 10


def test_case_result_entry_rejects_missing_case_key() -> None:
    with pytest.raises(ValidationError):
        CaseResultEntry.model_validate({"status": "passed"})


def test_case_result_entry_rejects_negative_duration() -> None:
    with pytest.raises(ValidationError):
        CaseResultEntry(case_key="c0", status="passed", duration_ms=-1)


def test_case_result_entry_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        CaseResultEntry(case_key="c0", status="bogus")
