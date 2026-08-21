"""崩溃恢复服务测试（§8.6）。"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from master.application.services.recovery_service import RecoveryService
from master.domain.enums import (
    DeviceStatus,
    RunStatus,
    ShardAttemptStatus,
    ShardStatus,
)
from master.domain.models import Device, RunShard, ShardAttempt, TaskRun
from master.domain.time import utcnow


def _mock_uow():
    """创建 mock UoW，__enter__ 返回自身。"""
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class TestHandleNodeOffline:
    def test_marks_active_attempts_failed_and_releases_devices(self):
        """节点离线：活跃 Attempt → failed，Shard → waiting_recovery，设备释放。"""
        uow = _mock_uow()

        attempt = ShardAttempt(
            id=1,
            attempt_id="ATT-1",
            shard_id="SH-1",
            attempt_no=1,
            node_id="node-1",
            device_ids=["dev-1"],
            status=ShardAttemptStatus.RUNNING,
        )
        uow.shard_attempts.list_active_by_node.return_value = [attempt]

        shard = RunShard(
            id=1,
            shard_id="SH-1",
            run_id="R-1",
            shard_index=0,
            status=ShardStatus.RUNNING,
        )
        uow.run_shards.get_by_shard_id.return_value = shard

        device = Device(id=1, device_id="dev-1", node_id="node-1", name="dev-1", online=True, status=DeviceStatus.BUSY)
        uow.devices.get_by_id.return_value = device

        service = RecoveryService(uow_factory=lambda: uow)
        handled = service.handle_node_offline("node-1")

        assert handled == 1
        assert attempt.status == ShardAttemptStatus.FAILED
        assert shard.status == ShardStatus.WAITING_RECOVERY
        assert device.status == DeviceStatus.ONLINE

    def test_no_active_attempts_is_noop(self):
        """节点离线但无活跃 Attempt → 不做任何处理。"""
        uow = _mock_uow()
        uow.shard_attempts.list_active_by_node.return_value = []

        service = RecoveryService(uow_factory=lambda: uow)
        handled = service.handle_node_offline("node-1")
        assert handled == 0


class TestStartupRecovery:
    def test_stale_runs_marked_timed_out(self):
        """启动恢复：超过超时阈值的非终态 Run → timed_out。"""
        uow = _mock_uow()
        stale_run = TaskRun(
            id=1,
            run_id="R-STALE",
            project_id="P-1",
            task_id="T-1",
            status=RunStatus.RUNNING,
            created_at=utcnow() - timedelta(hours=2),
        )
        uow.task_runs.list_non_terminal.return_value = [stale_run]
        uow.run_shards.list_by_run.return_value = []

        service = RecoveryService(
            uow_factory=lambda: uow,
            stale_timeout=timedelta(minutes=30),
        )
        stats = service.startup_recovery()

        assert stats["stale_runs"] == 1
        assert stale_run.status == RunStatus.TIMED_OUT

    def test_recent_runs_get_orphan_shards_recovered(self):
        """启动恢复：未超时但有活跃 Shard → 转为 waiting_recovery。"""
        uow = _mock_uow()
        recent_run = TaskRun(
            id=2,
            run_id="R-RECENT",
            project_id="P-1",
            task_id="T-1",
            status=RunStatus.RUNNING,
            created_at=utcnow() - timedelta(minutes=5),
        )
        uow.task_runs.list_non_terminal.return_value = [recent_run]

        active_shard = RunShard(
            id=1,
            shard_id="SH-1",
            run_id="R-RECENT",
            shard_index=0,
            status=ShardStatus.RUNNING,
        )
        uow.run_shards.list_by_run.return_value = [active_shard]

        service = RecoveryService(
            uow_factory=lambda: uow,
            stale_timeout=timedelta(minutes=30),
        )
        stats = service.startup_recovery()

        assert stats["stale_runs"] == 0
        assert stats["orphan_shards"] == 1
        assert active_shard.status == ShardStatus.WAITING_RECOVERY

    def test_no_non_terminal_runs_is_noop(self):
        """启动恢复：无非终态 Run → 仍重置节点投影（不关闭会话）。"""
        uow = _mock_uow()
        uow.nodes.mark_all_offline.return_value = 2
        uow.task_runs.list_non_terminal.return_value = []

        service = RecoveryService(uow_factory=lambda: uow)
        stats = service.startup_recovery()
        assert stats == {
            "stale_runs": 0,
            "orphan_shards": 0,
            "offline_nodes": 2,
        }
        # 节点投影重置被调用；会话不关闭（Agent 可能仍在线，§8.6）
        uow.nodes.mark_all_offline.assert_called_once()
        uow.node_sessions.close_all_open.assert_not_called()


class TestDetectStaleRuns:
    def test_detects_and_times_out_stale_runs(self):
        """超时检测：长时间无进展的 Run → timed_out + Shard 也超时。"""
        uow = _mock_uow()
        stale = TaskRun(
            id=1,
            run_id="R-STALE",
            project_id="P-1",
            task_id="T-1",
            status=RunStatus.DISPATCHED,
            created_at=utcnow() - timedelta(hours=1),
        )
        uow.task_runs.list_non_terminal.return_value = [stale]

        shard = RunShard(
            id=1,
            shard_id="SH-1",
            run_id="R-STALE",
            shard_index=0,
            status=ShardStatus.DISPATCHING,
        )
        uow.run_shards.list_by_run.return_value = [shard]

        service = RecoveryService(
            uow_factory=lambda: uow,
            stale_timeout=timedelta(minutes=30),
        )
        count = service.detect_stale_runs()

        assert count == 1
        assert stale.status == RunStatus.TIMED_OUT
        assert shard.status == ShardStatus.TIMED_OUT
