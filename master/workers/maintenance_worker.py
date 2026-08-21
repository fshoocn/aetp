"""后台维护 worker（P8.2/P8.5，§8.6）。

周期执行两类维护任务（同一异步循环，避免多线程/多事件循环脆弱模式）：

1. **Schedule tick**：触发所有到期的定时/周期调度计划（ScheduleService.tick）；
2. **Stale Run 检测**：把长时间无进展的非终态 Run 标记 timed_out
   （RecoveryService.detect_stale_runs）。

worker 只依赖应用服务（异步契约），不接触具体 MQTT/HTTP；由 Master
lifespan 启动/停止（与 OutboxWorker 一致的生命周期模式）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from master.application.services.recovery_service import RecoveryService
from master.application.services.schedule_service import ScheduleService

logger = logging.getLogger("master.workers.maintenance")


class MaintenanceWorker:
    """周期执行 Schedule 推进与 Stale Run 检测的后台 worker。"""

    def __init__(
        self,
        schedule_service: ScheduleService,
        recovery_service: RecoveryService,
        *,
        interval_s: float = 30.0,
    ) -> None:
        self._schedule = schedule_service
        self._recovery = recovery_service
        self._interval_s = interval_s
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动后台循环（幂等）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("维护 worker 启动（interval=%.1fs）", self._interval_s)

    async def stop(self) -> None:
        """停止循环（幂等）。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("维护 worker 停止")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("维护 worker 轮询异常")
            await asyncio.sleep(self._interval_s)

    async def run_once(self) -> dict[str, int]:
        """执行一轮维护（触发到期调度 + 检测超时 Run）。"""
        stats: dict[str, int] = {"schedules_triggered": 0, "stale_runs": 0}
        try:
            stats["schedules_triggered"] = await self._schedule.tick()
        except Exception:
            logger.exception("Schedule tick 失败")
        try:
            stats["stale_runs"] = self._recovery.detect_stale_runs()
        except Exception:
            logger.exception("Stale Run 检测失败")
        return stats
