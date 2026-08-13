"""执行编排层：WorkflowSpec + WorkflowEngine 测试。

验证：阶段推进（成功/失败/重试/超时）、WorkflowSpec 不变量校验、
阶段事件按序持久化（可观测/回放）。
"""

from __future__ import annotations

import asyncio

import pytest

from master.application.workflow_engine import WorkflowEngine
from master.domain.workflow import (
    SCRIPT_WORKFLOW,
    WorkflowProgress,
    WorkflowSpec,
    WorkflowStage,
)


class RecordingEventStore:
    """内存事件存储（模拟 EventStore 端口，P3.7）。"""

    def __init__(self) -> None:
        self.events = []

    def append(self, event):
        event.sequence = len(self.events) + 1
        self.events.append(event)
        return event


class ScriptRunner:
    """模拟动作执行：按 action 配置失败次数与超时挂起。"""

    def __init__(
        self,
        failures: dict[str, int] | None = None,
        hang_on: set[str] | None = None,
    ) -> None:
        self._failures = dict(failures or {})
        self._hang_on = hang_on or set()
        self.calls: list[str] = []

    async def run(self, action, context):
        self.calls.append(action)
        if action in self._hang_on:
            await asyncio.sleep(0.5)  # 挂起（超时由 stage.timeout_s 触发）
        if self._failures.get(action, 0) > 0:
            self._failures[action] -= 1
            return False
        return True


def _run(spec, runner, *, stage="uploaded"):
    engine = WorkflowEngine(RecordingEventStore())
    progress = WorkflowProgress(
        aggregate_id="S-1",
        stage=stage,
        context={"project_id": "p1"},
    )
    return asyncio.run(engine.advance(spec, progress, runner)), progress


# ---------------------------------------------------------------------------
# WorkflowSpec 纯函数与不变量
# ---------------------------------------------------------------------------


def test_spec_next_stage_success_path():
    assert SCRIPT_WORKFLOW.next_stage("uploaded", True) == "verify"
    assert SCRIPT_WORKFLOW.next_stage("verify", True) == "parse"
    assert SCRIPT_WORKFLOW.next_stage("parse", True) == "ready"


def test_spec_next_stage_failure_path():
    assert SCRIPT_WORKFLOW.next_stage("verify", False) == "failed"
    assert SCRIPT_WORKFLOW.next_stage("parse", False) == "failed"


def test_spec_start_and_terminal():
    progress = WorkflowProgress(aggregate_id="S-1", stage="ready")
    assert progress.is_terminal(SCRIPT_WORKFLOW) is True
    progress2 = WorkflowProgress(aggregate_id="S-1", stage="failed")
    assert progress2.is_terminal(SCRIPT_WORKFLOW) is True
    progress3 = WorkflowProgress(aggregate_id="S-1", stage="verify")
    assert progress3.is_terminal(SCRIPT_WORKFLOW) is False


def test_spec_invalid_invariants():
    # 起始阶段不存在
    with pytest.raises(ValueError, match="起始阶段不存在"):
        WorkflowSpec(
            aggregate_type="x", start="nope",
            stages={"a": WorkflowStage(name="a"), "b": WorkflowStage(name="b")},
            terminal_success="a", terminal_failure="b",
        )
    # 去向不存在（终态合法时才会走到去向校验）
    with pytest.raises(ValueError, match="去向不存在"):
        WorkflowSpec(
            aggregate_type="x", start="a",
            stages={
                "a": WorkflowStage(name="a", on_success="ghost"),
                "b": WorkflowStage(name="b"),
            },
            terminal_success="a", terminal_failure="b",
        )


# ---------------------------------------------------------------------------
# WorkflowEngine 推进
# ---------------------------------------------------------------------------


def test_engine_full_success():
    """全阶段成功：推进到 ready，动作按序执行，事件按序持久化。"""
    runner = ScriptRunner()
    progress, _ = _run(SCRIPT_WORKFLOW, runner)
    assert progress.stage == "ready"
    assert runner.calls == ["persist_script", "verify_script", "parse_cases"]
    assert progress.attempts == 1


def test_engine_retry_then_success():
    """verify 失败 1 次后重试成功（retry=2）：仍在重试预算内。"""
    runner = ScriptRunner(failures={"verify_script": 1})
    progress, _ = _run(SCRIPT_WORKFLOW, runner)
    assert progress.stage == "ready"
    assert runner.calls.count("verify_script") == 2  # 1 失败 + 1 重试成功


def test_engine_retry_exhausted_to_failed():
    """parse 重试耗尽（retry=1，连败 2 次）→ failed。"""
    runner = ScriptRunner(failures={"parse_cases": 2})
    progress, _ = _run(SCRIPT_WORKFLOW, runner)
    assert progress.stage == "failed"
    assert runner.calls.count("parse_cases") == 2
    assert progress.error is not None


def test_engine_timeout_fails_stage():
    """动作超时（stage.timeout_s）→ 阶段失败 → 进入失败终态。"""
    timeout_spec = WorkflowSpec(
        aggregate_type="script",
        start="uploaded",
        stages={
            "uploaded": WorkflowStage(name="uploaded", action="persist", on_success="verify"),
            "verify": WorkflowStage(
                name="verify", action="hang", timeout_s=0.1, retry=0, on_failure="failed",
            ),
            "ready": WorkflowStage(name="ready"),
            "failed": WorkflowStage(name="failed"),
        },
        terminal_success="ready",
        terminal_failure="failed",
    )
    runner = ScriptRunner(hang_on={"hang"})
    progress, _ = _run(timeout_spec, runner)
    assert progress.stage == "failed"
    assert "超时" in (progress.error or "")


def test_engine_events_emitted_in_order():
    """阶段事件按序持久化（可观测/回放）：entered → succeeded ... → workflow.succeeded。"""
    store = RecordingEventStore()
    engine = WorkflowEngine(store)
    runner = ScriptRunner()
    progress = WorkflowProgress(
        aggregate_id="S-1", stage="uploaded", context={"project_id": "p1"}
    )
    asyncio.run(engine.advance(SCRIPT_WORKFLOW, progress, runner))
    types = [e.event_type for e in store.events]
    assert types == [
        "script.stage_entered",
        "script.stage_succeeded",
        "script.stage_entered",
        "script.stage_succeeded",
        "script.stage_entered",
        "script.stage_succeeded",
        "script.workflow.succeeded",
    ]
    assert all(e.sequence is not None for e in store.events)  # 单调序号


def test_engine_failure_events_and_error():
    """失败路径：stage_failed + workflow.failed，error 记录在事件载荷。"""
    store = RecordingEventStore()
    engine = WorkflowEngine(store)
    runner = ScriptRunner(failures={"verify_script": 3})  # retry=2 耗尽
    progress = WorkflowProgress(
        aggregate_id="S-1", stage="uploaded", context={"project_id": "p1"}
    )
    asyncio.run(engine.advance(SCRIPT_WORKFLOW, progress, runner))
    assert progress.stage == "failed"
    types = [e.event_type for e in store.events]
    assert types[-1] == "script.workflow.failed"
    assert "script.stage_failed" in types
    failed_payload = next(
        e.payload for e in store.events if e.event_type == "script.stage_failed"
    )
    assert failed_payload["stage"] == "verify"
    assert failed_payload["attempts"] == 3  # 1 次 + 2 次重试
