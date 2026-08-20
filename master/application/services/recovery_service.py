"""崩溃恢复服务（§8.6，Master 重启 + 节点离线恢复）。

处理三类恢复场景：
1. 节点离线（LWT）→ 该节点上活跃 Shard 转 waiting_recovery + failover 重调度
2. Master 启动恢复 → 扫描遗留非终态 Run/Shard，超时标记 lost/timed_out
3. 定期巡检 → 长时间无进展的 Run 标记超时
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable

from master.domain.enums import (
    DeviceStatus,
    RunStatus,
    ShardAttemptStatus,
    ShardStatus,
)
from master.domain.models import ShardAttempt
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)

# Run 超时阈值：超过此时间仍无终态 → timed_out
_RUN_STALE_TIMEOUT = timedelta(minutes=30)


class RecoveryService:
    """崩溃恢复服务：节点离线恢复 + 启动扫描 + 超时检测。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        stale_timeout: timedelta = _RUN_STALE_TIMEOUT,
    ) -> None:
        self._uow_factory = uow_factory
        self._stale_timeout = stale_timeout

    # ── 1. 节点离线恢复（LWT 触发） ────────────────────────────────────

    def handle_node_offline(self, node_id: str) -> int:
        """节点离线时，将其上所有活跃 Attempt 标记为 failed，
        对应 Shard 转为 waiting_recovery 并释放设备。

        Returns:
            处理的 Attempt 数量
        """
        now = utcnow()
        handled = 0

        with self._uow_factory() as uow:
            active_attempts = uow.shard_attempts.list_active_by_node(node_id)
            if not active_attempts:
                return 0

            for attempt in active_attempts:
                attempt.status = ShardAttemptStatus.FAILED
                attempt.error_code = "NODE_OFFLINE"
                attempt.error_message = f"节点 {node_id} 离线"
                attempt.finished_at = now
                uow.shard_attempts.update(attempt)

                shard = uow.run_shards.get_by_shard_id(attempt.shard_id)
                if shard is not None and shard.status in {
                    ShardStatus.DISPATCHING,
                    ShardStatus.RUNNING,
                }:
                    shard.status = ShardStatus.WAITING_RECOVERY
                    uow.run_shards.update(shard)

                for device_id in attempt.device_ids:
                    device = uow.devices.get_by_id(device_id)
                    if device is not None:
                        device.status = (
                            DeviceStatus.ONLINE
                            if device.online
                            else DeviceStatus.OFFLINE
                        )
                        uow.devices.update(device)

                handled += 1

        logger.info(
            "节点离线恢复完成: node=%s attempts_failed=%d", node_id, handled
        )
        return handled

    # ── 2. Master 启动恢复 ────────────────────────────────────────────

    def startup_recovery(self) -> dict[str, int]:
        """Master 启动时扫描遗留非终态 Run，处理超时和孤儿 Attempt。

        Returns:
            各类处理的数量统计
        """
        now = utcnow()
        stats = {"stale_runs": 0, "orphan_shards": 0}

        with self._uow_factory() as uow:
            # 扫描所有非终态 Run
            non_terminal_runs = uow.task_runs.list_non_terminal(limit=5000)
            if not non_terminal_runs:
                logger.info("启动恢复：无遗留非终态 Run")
                return stats

            logger.warning(
                "启动恢复：发现 %d 个遗留非终态 Run", len(non_terminal_runs)
            )

            for run in non_terminal_runs:
                if run.created_at and now - run.created_at > self._stale_timeout:
                    run.status = RunStatus.TIMED_OUT
                    run.finished_at = now
                    uow.task_runs.update(run)
                    stats["stale_runs"] += 1
                    continue

                # 将非终态 Run 上的活跃 Shard 标记为等待恢复
                shards = uow.run_shards.list_by_run(run.run_id)
                for shard in shards:
                    if shard.status in {
                        ShardStatus.DISPATCHING,
                        ShardStatus.RUNNING,
                    }:
                        shard.status = ShardStatus.WAITING_RECOVERY
                        uow.run_shards.update(shard)
                        stats["orphan_shards"] += 1

        if stats["stale_runs"] or stats["orphan_shards"]:
            logger.warning("启动恢复完成: %s", stats)
        else:
            logger.info("启动恢复：所有 Run 状态正常")

        return stats

    # ── 3. Stale Run 超时巡检 ─────────────────────────────────────────

    def detect_stale_runs(self) -> int:
        """定期检测长时间无进展的 Run，标记为 timed_out。

        Returns:
            超时处理的 Run 数量
        """
        now = utcnow()
        timed_out = 0

        with self._uow_factory() as uow:
            non_terminal = uow.task_runs.list_non_terminal(limit=5000)
            for run in non_terminal:
                if run.created_at and now - run.created_at > self._stale_timeout:
                    run.status = RunStatus.TIMED_OUT
                    run.finished_at = now
                    uow.task_runs.update(run)
                    timed_out += 1

                    shards = uow.run_shards.list_by_run(run.run_id)
                    for shard in shards:
                        if shard.status not in {
                            ShardStatus.SUCCEEDED,
                            ShardStatus.FAILED,
                            ShardStatus.CANCELLED,
                            ShardStatus.TIMED_OUT,
                        }:
                            shard.status = ShardStatus.TIMED_OUT
                            uow.run_shards.update(shard)

        if timed_out:
            logger.warning("超时检测：%d 个 Run 标记为 timed_out", timed_out)
        return timed_out
