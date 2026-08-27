"""后台维护 worker 测试（P8.2/P8.5，§8.6）。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from master.workers.maintenance_worker import MaintenanceWorker


def test_run_once_triggers_schedule_and_stale_detection():
    """一轮维护同时触发 Schedule tick 与 Stale Run 检测。"""
    schedule = MagicMock()
    schedule.tick = AsyncMock(return_value=2)
    recovery = MagicMock()
    recovery.detect_stale_runs = MagicMock(return_value=3)

    worker = MaintenanceWorker(schedule, recovery)
    stats = asyncio.run(worker.run_once())

    assert stats == {"schedules_triggered": 2, "stale_runs": 3, "orphans_removed": 0}
    schedule.tick.assert_awaited_once()
    recovery.detect_stale_runs.assert_called_once()


def test_run_once_isolates_schedule_failure():
    """Schedule tick 失败不影响 Stale Run 检测（fail-open）。"""
    schedule = MagicMock()
    schedule.tick = AsyncMock(side_effect=RuntimeError("tick boom"))
    recovery = MagicMock()
    recovery.detect_stale_runs = MagicMock(return_value=1)

    worker = MaintenanceWorker(schedule, recovery)
    stats = asyncio.run(worker.run_once())

    assert stats == {"schedules_triggered": 0, "stale_runs": 1, "orphans_removed": 0}
    recovery.detect_stale_runs.assert_called_once()


def test_run_once_isolates_stale_failure():
    """Stale 检测失败不影响 Schedule tick（fail-open）。"""
    schedule = MagicMock()
    schedule.tick = AsyncMock(return_value=5)
    recovery = MagicMock()
    recovery.detect_stale_runs = MagicMock(side_effect=RuntimeError("stale boom"))

    worker = MaintenanceWorker(schedule, recovery)
    stats = asyncio.run(worker.run_once())

    assert stats == {"schedules_triggered": 5, "stale_runs": 0, "orphans_removed": 0}
    schedule.tick.assert_awaited_once()


def test_start_stop_is_idempotent():
    """start/stop 幂等，不重复创建后台任务。"""
    schedule = MagicMock()
    schedule.tick = AsyncMock(return_value=0)
    recovery = MagicMock()
    recovery.detect_stale_runs = MagicMock(return_value=0)

    worker = MaintenanceWorker(schedule, recovery, interval_s=0.001)

    async def _lifecycle():
        await worker.start()
        await worker.start()  # 幂等
        await worker.stop()
        await worker.stop()  # 幂等

    asyncio.run(_lifecycle())
    assert worker._task is None
