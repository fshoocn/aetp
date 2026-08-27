"""P6.2：Agent TaskContext（progress/log/case-status 上报 + RunLogBatch 生成）测试。

验收要点：
1. log 写入 spool，(run_id, sequence) 幂等（重复 sequence 不重复写）
2. progress 生成 run.progress 入 outbox，sequence 单调递增
3. case_status 生成 run.case-status 入 outbox，同 case 幂等覆盖
4. build_log_batch 生成 RunLogBatch，严格按 sequence 递增、first_sequence=首条
5. capture_log 标注 stream；raise_if_cancelled 命中取消信号抛 ExecutionCancelled
6. 协议层 RunLogEntry/RunLogBatch 非法序列拒绝
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aetp_protocol.envelope import Envelope
from aetp_protocol.logs import LogLevel, RunLogBatch, RunLogEntry
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import RunCaseStatusPayload, RunProgressPayload
from pydantic import ValidationError

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.execution_service import ExecutionCancelled
from agent.application.services.task_context import TaskContext
from agent.config import AgentSettings
from agent.domain.ledger import TaskLogSpoolEntry


def _now() -> datetime:
    return datetime(2099, 1, 1, tzinfo=UTC)


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
)


def _make_context(tmp_path, *, cancelled: bool = False) -> tuple[TaskContext, SQLiteLedger]:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    ctx = TaskContext(
        _SETTINGS,
        ledger,
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        run_id="R-1",
        is_cancelled=lambda: cancelled,
        session_id=lambda: "sess-1",
        now=_now,
    )
    return ctx, ledger


# -----------------------------------------------------------------------
# log / capture_log
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_writes_spool(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path)
    await ctx.log("info", "hello")
    await ctx.log("warn", "watch out", {"k": 1})

    pending = ctx.collect_pending_logs(10)
    assert [e.sequence for e in pending] == [1, 2]
    assert pending[0].level == "info"
    assert pending[0].message == "hello"
    assert pending[1].level == "warn"
    assert pending[1].detail == {"k": 1}


@pytest.mark.asyncio
async def test_collect_pending_logs_filters_before_limit(tmp_path) -> None:
    ctx, ledger = _make_context(tmp_path)
    ledger.append_task_log(TaskLogSpoolEntry("R-1", 1, "info", "other-1"))
    ledger.append_task_log(TaskLogSpoolEntry("R-1", 2, "info", "other-2"))
    ledger.append_task_log(TaskLogSpoolEntry("R-1", 3, "info", "current"))

    # 当前 Run 的查询在数据库层过滤，不能被其他 Run 的日志占满 limit。
    ctx = TaskContext(
        _SETTINGS,
        ledger,
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        run_id="R-1",
        now=_now,
    )
    pending = ctx.collect_pending_logs(1)
    assert [entry.message for entry in pending] == ["other-1"]

    other_ctx = TaskContext(
        _SETTINGS,
        ledger,
        project_id="p1",
        task_id="T-2",
        shard_id="SH-2",
        run_id="R-2",
        now=_now,
    )
    ledger.append_task_log(TaskLogSpoolEntry("R-2", 1, "info", "other-run"))
    assert [entry.message for entry in other_ctx.collect_pending_logs(1)] == ["other-run"]


@pytest.mark.asyncio
async def test_log_sequence_idempotent_by_ledger(tmp_path) -> None:
    """(run_id, sequence) 幂等由账本唯一约束保证，重复 sequence 不重复落库。"""
    ctx, ledger = _make_context(tmp_path)
    await ctx.log("info", "first")

    # 直接向账本追加同 sequence：被唯一约束忽略
    ledger.append_task_log(TaskLogSpoolEntry(run_id="R-1", sequence=1, level="info", message="dup"))
    assert len(ctx.collect_pending_logs(10)) == 1


@pytest.mark.asyncio
async def test_capture_log_marks_stream(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path)
    await ctx.capture_log("stdout", "line", {"raw": "x"})

    pending = ctx.collect_pending_logs(10)
    assert pending[0].detail == {"raw": "x", "stream": "stdout"}


@pytest.mark.asyncio
async def test_empty_captured_log_is_ignored(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path)
    await ctx.capture_log("stdout", "")
    await ctx.capture_log("stdout", "line")

    pending = ctx.collect_pending_logs(10)
    assert len(pending) == 1
    assert pending[0].sequence == 1
    assert pending[0].message == "line"


@pytest.mark.asyncio
async def test_log_rejects_invalid_level(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path)
    with pytest.raises(ValueError, match="非法日志等级"):
        await ctx.log("bogus", "bad level")


# -----------------------------------------------------------------------
# progress / case_status
# -----------------------------------------------------------------------


def _claim_outbox_envelopes(ledger, count: int) -> list[Envelope]:
    pending = ledger.claim_due_outbox(count, datetime(2099, 1, 1, tzinfo=UTC).replace(tzinfo=None))
    return [Envelope.model_validate(e.payload) for e in pending]


@pytest.mark.asyncio
async def test_progress_enqueues_run_progress(tmp_path) -> None:
    ctx, ledger = _make_context(tmp_path)
    await ctx.progress(50, "running", "half")

    envelopes = _claim_outbox_envelopes(ledger, 10)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.message_type == MessageType.RUN_PROGRESS.value
    assert RunProgressPayload.model_validate(env.payload).percent == 50
    assert env.payload["sequence"] == 1
    assert env.payload["run_id"] == "R-1"


@pytest.mark.asyncio
async def test_case_status_enqueues_run_case_status(tmp_path) -> None:
    ctx, ledger = _make_context(tmp_path)
    await ctx.case_status("case-1", "passed")

    envelopes = _claim_outbox_envelopes(ledger, 10)
    assert envelopes[0].message_type == MessageType.RUN_CASE_STATUS.value
    assert RunCaseStatusPayload.model_validate(envelopes[0].payload).status == "passed"


# -----------------------------------------------------------------------
# RunLogBatch 生成
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_log_batch_strict_sequence(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path)
    await ctx.log("info", "a")
    await ctx.log("error", "b")
    await ctx.log("debug", "c")

    entries = ctx.collect_pending_logs(10)
    batch = ctx.build_log_batch(entries)
    assert batch is not None
    assert batch.run_id == "R-1"
    assert batch.first_sequence == 1
    assert [e.sequence for e in batch.entries] == [1, 2, 3]
    assert [e.level for e in batch.entries] == [LogLevel.INFO, LogLevel.ERROR, LogLevel.DEBUG]
    assert batch.entries[0].project_id == "p1"
    assert batch.entries[0].node_id == "bench-001"


@pytest.mark.asyncio
async def test_build_log_batch_empty_returns_none(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path)
    assert ctx.build_log_batch([]) is None


@pytest.mark.asyncio
async def test_mark_logs_published(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path)
    await ctx.log("info", "a")
    entries = ctx.collect_pending_logs(10)
    ctx.mark_logs_published([e.id for e in entries if e.id is not None])
    assert ctx.collect_pending_logs(10) == []


# -----------------------------------------------------------------------
# 取消信号
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raise_if_cancelled(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path, cancelled=True)
    with pytest.raises(ExecutionCancelled):
        await ctx.raise_if_cancelled()


@pytest.mark.asyncio
async def test_raise_if_not_cancelled_passes(tmp_path) -> None:
    ctx, _ledger = _make_context(tmp_path, cancelled=False)
    await ctx.raise_if_cancelled()  # 不抛


# -----------------------------------------------------------------------
# 协议层：RunLogEntry/RunLogBatch 校验
# -----------------------------------------------------------------------


def test_run_log_batch_rejects_non_strict_sequence() -> None:
    entry = {
        "project_id": "p1",
        "task_id": "T-1",
        "run_id": "R-1",
        "node_id": "bench-001",
        "sequence": 2,
        "level": "info",
        "message": "x",
        "occurred_at": "2026-08-17T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        RunLogBatch(
            run_id="R-1",
            first_sequence=1,  # 与首条 sequence=2 不一致
            entries=[RunLogEntry(**entry)],
        )


def test_run_log_batch_rejects_non_increasing() -> None:
    base = {
        "project_id": "p1",
        "task_id": "T-1",
        "run_id": "R-1",
        "node_id": "bench-001",
        "level": "info",
        "message": "x",
        "occurred_at": "2026-08-17T12:00:00Z",
    }
    entries = [
        RunLogEntry(**{**base, "sequence": 2}),
        RunLogEntry(**{**base, "sequence": 1}),  # 递减
    ]
    with pytest.raises(ValidationError):
        RunLogBatch(run_id="R-1", first_sequence=2, entries=entries)


def test_run_log_batch_rejects_unknown_level() -> None:
    base = {
        "project_id": "p1",
        "task_id": "T-1",
        "run_id": "R-1",
        "node_id": "bench-001",
        "sequence": 1,
        "message": "x",
        "occurred_at": "2026-08-17T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        RunLogEntry(**{**base, "level": "bogus"})
