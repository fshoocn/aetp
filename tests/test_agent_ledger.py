"""P5.2：Agent 本地账本（SQLite）与原子 claim 测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.domain.enums import AgentOutboxStatus, AgentRunStatus
from agent.domain.ledger import ScriptCacheEntry, TaskLogSpoolEntry


def _ledger(tmp_path, *, max_spool_bytes: int = 104857600) -> SQLiteLedger:
    return SQLiteLedger(
        f"sqlite:///{tmp_path / 'agent.db'}",
        max_spool_bytes=max_spool_bytes,
    )


def _future_utc(seconds: int = 60) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=seconds)


def test_claim_run_is_atomic(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    assert ledger.claim_run("run-1", 1) is True
    # 同一 run 同一 attempt 重复派发：不二次执行
    assert ledger.claim_run("run-1", 1) is False
    # 当前 attempt 尚未失败时，不接受未来 attempt
    assert ledger.claim_run("run-1", 2) is False
    run = ledger.get_run("run-1")
    assert run is not None
    run.status = AgentRunStatus.FAILED
    ledger.update_run(run)
    # 失败后的新 attempt（D-20 failover）：接受
    assert ledger.claim_run("run-1", 2) is True
    # 迟到的旧 attempt 不得覆盖当前 attempt
    assert ledger.claim_run("run-1", 1) is False


def test_claim_run_persists_device_ids(tmp_path) -> None:
    """claim 时记录占用的物理设备集合，供心跳汇总占用状态（§9.8）。"""
    ledger = _ledger(tmp_path)
    assert ledger.claim_run("run-1", 1, ["can1", "relay-board-2"]) is True
    run = ledger.get_run("run-1")
    assert run is not None
    assert run.device_ids == ["can1", "relay-board-2"]

    # failover 新 attempt 更新 device_ids
    run.status = AgentRunStatus.FAILED
    ledger.update_run(run)
    assert ledger.claim_run("run-1", 2, ["can2"]) is True
    updated = ledger.get_run("run-1")
    assert updated is not None
    assert updated.device_ids == ["can2"]


def test_get_and_update_run(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.claim_run("run-1", 1)

    run = ledger.get_run("run-1")
    assert run is not None
    assert run.status is AgentRunStatus.CLAIMED
    assert run.attempt_no == 1

    run.status = AgentRunStatus.RUNNING
    run.cancelled = True
    ledger.update_run(run)

    updated = ledger.get_run("run-1")
    assert updated is not None
    assert updated.status is AgentRunStatus.RUNNING
    assert updated.cancelled is True


def test_get_missing_run_returns_none(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    assert ledger.get_run("no-such-run") is None


def test_inbox_dedup(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    assert ledger.record_inbox("master", "msg-1", "run.assign") is True
    assert ledger.record_inbox("master", "msg-1", "run.assign") is False
    assert ledger.record_inbox("master", "msg-2", "run.assign") is True


def test_outbox_roundtrip(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.enqueue_outbox("o-1", "aetp/v2/topic", {"k": "v"})

    due = ledger.claim_due_outbox(10, _future_utc())
    assert len(due) == 1
    assert due[0].outbox_id == "o-1"
    assert due[0].status is AgentOutboxStatus.SENDING
    assert due[0].claimed_until is not None

    ledger.mark_outbox(
        "o-1",
        status=AgentOutboxStatus.SENT,
        attempts=1,
        next_attempt_at=None,
    )
    assert ledger.claim_due_outbox(10, _future_utc()) == []


def test_task_log_spool_is_idempotent(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    entry = TaskLogSpoolEntry(run_id="r1", sequence=1, level="info", message="hello")
    ledger.append_task_log(entry)
    # 同 (run_id, sequence) 重复追加：幂等忽略
    ledger.append_task_log(entry)

    pending = ledger.list_pending_task_logs(10)
    assert len(pending) == 1
    assert pending[0].message == "hello"
    assert pending[0].published is False
    assert pending[0].id is not None

    ledger.mark_task_logs_published([pending[0].id])
    assert ledger.list_pending_task_logs(10) == []


def test_script_cache_by_hash(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    entry = ScriptCacheEntry(script_id="S-1", version=1, sha256="a" * 64, path="/cache/S-1.zip")
    assert ledger.cache_script(entry) is True
    # 同 (script_id, version, sha256) 重复缓存：幂等返回 False
    assert ledger.cache_script(entry) is False

    cached = ledger.get_cached_script("S-1", 1, "a" * 64)
    assert cached is not None
    assert cached.path == "/cache/S-1.zip"

    assert ledger.get_cached_script("S-1", 1, "b" * 64) is None


def test_task_log_spool_evicts_low_level_but_keeps_error(tmp_path) -> None:
    ledger = _ledger(tmp_path, max_spool_bytes=20)
    ledger.append_task_log(TaskLogSpoolEntry(run_id="r1", sequence=1, level="info", message="old-info"))
    ledger.append_task_log(TaskLogSpoolEntry(run_id="r1", sequence=2, level="error", message="important-error"))
    pending = ledger.list_pending_task_logs(10)
    assert [(entry.sequence, entry.level) for entry in pending] == [(2, "error")]

    # 即使超过上限，error 也必须保留。
    ledger.append_task_log(TaskLogSpoolEntry(run_id="r1", sequence=3, level="error", message="second-error"))
    assert [entry.sequence for entry in ledger.list_pending_task_logs(10)] == [
        2,
        3,
    ]
