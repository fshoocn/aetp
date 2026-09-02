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

from master.application.services.notification_dispatcher import NotificationDispatcher
from master.application.services.plan_lease_service import PlanLeaseService
from master.application.services.recovery_service import RecoveryService
from master.application.services.schedule_service import ScheduleService
from master.application.services.storage_cleanup_service import StorageCleanupService

logger = logging.getLogger("master.workers.maintenance")


class MaintenanceWorker:
    """周期执行 Schedule 推进、Stale Run 检测与存储孤儿清理的后台 worker。"""

    def __init__(
        self,
        schedule_service: ScheduleService,
        recovery_service: RecoveryService,
        *,
        storage_cleanup_service: StorageCleanupService | None = None,
        plan_lease_service: PlanLeaseService | None = None,
        interval_s: float = 30.0,
        notification_dispatcher: NotificationDispatcher | None = None,
    ) -> None:
        self._schedule = schedule_service
        self._recovery = recovery_service
        self._storage_cleanup = storage_cleanup_service
        self._plan_leases = plan_lease_service
        self._notification_dispatcher = notification_dispatcher
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
        """执行一轮维护（触发到期调度 + 检测超时 Run + 清理孤儿文件）。"""
        stats: dict[str, int] = {"schedules_triggered": 0, "stale_runs": 0, "orphans_removed": 0}
        if self._plan_leases is not None:
            stats["leases_expired"] = 0
        try:
            stats["schedules_triggered"] = await self._schedule.tick()
        except Exception:
            logger.exception("Schedule tick 失败")
        try:
            stats["stale_runs"] = self._recovery.detect_stale_runs()
        except Exception:
            logger.exception("Stale Run 检测失败")
        if self._plan_leases is not None:
            try:
                stats["leases_expired"] = len(self._plan_leases.expire_due())
            except Exception:
                logger.exception("V2 Lease 到期回收失败")
        if self._storage_cleanup is not None:
            try:
                stats["orphans_removed"] = self._storage_cleanup.cleanup_orphans()["removed"]
            except Exception:
                logger.exception("存储孤儿清理失败")
        if self._notification_dispatcher is not None:
            try:
                stats["notifications_flushed"] = await self._notification_dispatcher.flush_due()
            except Exception:
                logger.exception("通知 Digest 刷新失败")
        return stats
