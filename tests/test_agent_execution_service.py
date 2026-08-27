"""P6.1：Agent ExecutionService 测试（并发上限、timeout、cancel token、异常映射）。

验收要点（§15.3 P6.1：取消/超时语义正确）：
1. 正常执行 → SUCCEEDED 并回写账本
2. 插件异常 → FAILED（异常映射）
3. 超时 → TIMED_OUT
4. cancel（排队中）→ CANCELLED（获得槽位前检查 token）
5. cancel（执行中）→ CANCELLED（取消信号与插件任务竞争）
6. 并发上限：max_concurrent_runs 约束同时执行数
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.execution_service import (
    CancellationToken,
    ExecutionCancelled,
    ExecutionService,
)
from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus


def _now() -> datetime:
    return datetime(2099, 1, 1, tzinfo=UTC).replace(tzinfo=None)


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
    max_concurrent_runs=1,
)


class _ImmediatePlugin:
    """立即返回固定结果的插件。"""

    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"status": "passed"}

    async def execute(self, context):
        return self.result


class _Plugin:
    """可控制执行时长/结果/异常的插件替身（并发/取消测试用）。"""

    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"status": "passed"}
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, context):
        self.started.set()
        await self.release.wait()
        return self.result


class _RaisingPlugin:
    async def execute(self, context):
        raise RuntimeError("boom")


class _SlowPlugin:
    async def execute(self, context):
        await asyncio.sleep(10)
        return {"status": "passed"}


class _CancellablePlugin(_Plugin):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_called = False

    async def cancel(self) -> None:
        self.cancel_called = True


class _Context:
    pass


def _service(tmp_path, *, max_concurrent: int = 1) -> tuple[ExecutionService, SQLiteLedger]:
    settings = AgentSettings(
        node_id="bench-001",
        name="bench",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-001",
        mqtt_use_tls=False,
        max_concurrent_runs=max_concurrent,
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    return ExecutionService(settings, ledger), ledger


def _claim(ledger, run_id: str) -> None:
    assert ledger.claim_run(run_id, 1) is True


# -----------------------------------------------------------------------
# 正常执行与异常映射
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_success_marks_succeeded(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    _claim(ledger, "R-1")

    result = await service.execute("R-1", _ImmediatePlugin({"ok": True}), _Context())

    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.summary == {"ok": True}
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.status is AgentRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_execute_exception_maps_to_failed(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    _claim(ledger, "R-1")

    result = await service.execute("R-1", _RaisingPlugin(), _Context())

    assert result.status is AgentRunStatus.FAILED
    assert "boom" in result.error
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.status is AgentRunStatus.FAILED


# -----------------------------------------------------------------------
# 超时
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_timeout_maps_to_timed_out(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    _claim(ledger, "R-1")

    result = await service.execute("R-1", _SlowPlugin(), _Context(), timeout_s=1)

    assert result.status is AgentRunStatus.TIMED_OUT
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.status is AgentRunStatus.TIMED_OUT


@pytest.mark.asyncio
async def test_execute_timeout_zero_means_unlimited(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    _claim(ledger, "R-1")

    plugin = _ImmediatePlugin({"ok": True})
    task = asyncio.create_task(service.execute("R-1", plugin, _Context(), timeout_s=0))
    await task  # 立即完成，验证 timeout_s=0 不限制
    assert task.result().status is AgentRunStatus.SUCCEEDED


# -----------------------------------------------------------------------
# 取消
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_queued_run_maps_to_cancelled(tmp_path) -> None:
    service, ledger = _service(tmp_path, max_concurrent=1)
    _claim(ledger, "R-1")
    _claim(ledger, "R-2")

    blocker = _Plugin({"ok": True})
    task1 = asyncio.create_task(service.execute("R-1", blocker, _Context()))
    await blocker.started.wait()  # R-1 占住唯一槽位

    # R-2 排队等待槽位，此时取消
    task2 = asyncio.create_task(service.execute("R-2", _Plugin(), _Context()))
    await asyncio.sleep(0.02)
    assert service.cancel("R-2") is True

    # R-2 获得槽位前检查 token，立即以 CANCELLED 结束（无需等 R-1）
    result2 = await task2
    assert result2.status is AgentRunStatus.CANCELLED

    blocker.release.set()
    result1 = await task1
    assert result1.status is AgentRunStatus.SUCCEEDED
    run = ledger.get_run("R-2")
    assert run is not None
    assert run.status is AgentRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_running_run_maps_to_cancelled(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    _claim(ledger, "R-1")

    plugin = _Plugin()
    task = asyncio.create_task(service.execute("R-1", plugin, _Context()))
    await plugin.started.wait()

    assert service.cancel("R-1") is True
    result = await task
    assert result.status is AgentRunStatus.CANCELLED
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.status is AgentRunStatus.CANCELLED
    assert run.cancelled is True


@pytest.mark.asyncio
async def test_cancel_running_run_calls_plugin_cancel_hook(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    _claim(ledger, "R-1")

    plugin = _CancellablePlugin()
    task = asyncio.create_task(service.execute("R-1", plugin, _Context()))
    await plugin.started.wait()

    service.cancel("R-1")
    result = await task
    assert result.status is AgentRunStatus.CANCELLED
    assert plugin.cancel_called is True


def test_cancel_does_not_mark_terminal_run_cancelled(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    _claim(ledger, "R-1")
    run = ledger.get_run("R-1")
    assert run is not None
    run.status = AgentRunStatus.SUCCEEDED
    ledger.update_run(run)

    assert service.cancel("R-1") is False
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.cancelled is False


@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_false_but_sets_flag(tmp_path) -> None:
    service, ledger = _service(tmp_path)
    _claim(ledger, "R-1")

    # 无活跃 token：返回 False，但账本 cancelled 仍置位（run.cancel 语义）
    assert service.cancel("R-1") is False
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.cancelled is True


# -----------------------------------------------------------------------
# 并发上限
# -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_concurrent_runs_enforced(tmp_path) -> None:
    service, ledger = _service(tmp_path, max_concurrent=2)
    for run_id in ("R-1", "R-2", "R-3"):
        _claim(ledger, run_id)

    plugins = {rid: _Plugin() for rid in ("R-1", "R-2", "R-3")}
    tasks = {rid: asyncio.create_task(service.execute(rid, plugins[rid], _Context())) for rid in ("R-1", "R-2", "R-3")}

    # 等待前两个真正开始执行
    await asyncio.wait(
        [
            asyncio.create_task(plugins["R-1"].started.wait()),
            asyncio.create_task(plugins["R-2"].started.wait()),
        ],
        timeout=1,
    )
    await asyncio.sleep(0.02)
    # 上限 2：R-3 尚未获得槽位
    assert service.running == frozenset({"R-1", "R-2"})
    assert not plugins["R-3"].started.is_set()

    # 释放一个槽位后 R-3 进入
    plugins["R-1"].release.set()
    await plugins["R-3"].started.wait()
    assert "R-3" in service.running

    plugins["R-2"].release.set()
    plugins["R-3"].release.set()
    results = await asyncio.gather(*tasks.values())
    assert {r.status for r in results} == {AgentRunStatus.SUCCEEDED}


# -----------------------------------------------------------------------
# CancellationToken 单元语义
# -----------------------------------------------------------------------


def test_cancellation_token_raise_if_cancelled() -> None:
    token = CancellationToken()
    token.raise_if_cancelled()  # 未取消不抛
    token.cancel()
    with pytest.raises(ExecutionCancelled):
        token.raise_if_cancelled()


def test_execution_service_rejects_zero_concurrency(tmp_path) -> None:
    with pytest.raises(ValueError):
        ExecutionService(
            AgentSettings(
                node_id="bench-001",
                name="bench",
                master_id="aetp-master",
                mqtt_client_id="aetp-agent-bench-001",
                mqtt_use_tls=False,
                max_concurrent_runs=0,
            ),
            SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}"),
        )
