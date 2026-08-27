"""P3.6：纯函数状态机测试。

穷举 4 个状态机（Task/Run/Shard/Attempt）的全部 (from, to) 组合：
合法迁移可通过、非法迁移抛 InvalidStateTransitionError；并验证 D-20
三层重试在状态机层面的语义（failover=新 attempt，历史终态不迁移）。
"""

from __future__ import annotations

import pytest

from master.domain.enums import (
    RunStatus,
    ShardAttemptStatus,
    ShardStatus,
)
from master.domain.state_machine import (
    InvalidStateTransitionError,
    assert_transition,
    can_transition,
    is_terminal,
    next_attempt_no,
    transitions_for,
)

_ALL_ENUMS = [RunStatus, ShardStatus, ShardAttemptStatus]


@pytest.mark.parametrize("enum_type", _ALL_ENUMS)
def test_exhaustive_transitions(enum_type):
    """穷举：对每个状态机的全部 (current, target) 组合验证迁移合法性。"""
    for current in enum_type:
        for target in enum_type:
            allowed = target in transitions_for(current)
            assert can_transition(current, target) is allowed
            if allowed:
                assert_transition(current, target)  # 合法不抛
            else:
                with pytest.raises(InvalidStateTransitionError):
                    assert_transition(current, target)


@pytest.mark.parametrize("enum_type", _ALL_ENUMS)
def test_terminal_states_immutable(enum_type):
    """终态不可再迁移。"""
    for status in enum_type:
        for target in enum_type:
            if is_terminal(status):
                assert can_transition(status, target) is False


@pytest.mark.parametrize("enum_type", _ALL_ENUMS)
def test_initial_not_terminal(enum_type):
    """初始状态（第一个枚举值）非终态。"""
    first = next(iter(enum_type))
    assert is_terminal(first) is False


def test_cross_type_transition_rejected():
    """跨状态机类型迁移一律拒绝。"""
    assert can_transition(RunStatus.RUNNING, ShardStatus.RUNNING) is False
    assert can_transition(ShardStatus.RUNNING, ShardAttemptStatus.RUNNING) is False
    with pytest.raises(InvalidStateTransitionError):
        assert_transition(ShardStatus.PENDING, RunStatus.CREATED)


# ---------------------------------------------------------------------------
# 各状态机代表性流程
# ---------------------------------------------------------------------------


def test_run_typical_flow():
    assert can_transition(RunStatus.CREATED, RunStatus.DISPATCHED)
    assert can_transition(RunStatus.DISPATCHED, RunStatus.ACKED)
    assert can_transition(RunStatus.ACKED, RunStatus.RUNNING)
    assert can_transition(RunStatus.RUNNING, RunStatus.SUCCEEDED)
    assert can_transition(RunStatus.RUNNING, RunStatus.FAILED)
    assert can_transition(RunStatus.RUNNING, RunStatus.CANCELLED)
    assert can_transition(RunStatus.RUNNING, RunStatus.TIMED_OUT)
    assert can_transition(RunStatus.RUNNING, RunStatus.LOST)  # 节点离线丢失
    assert not can_transition(RunStatus.CREATED, RunStatus.SUCCEEDED)


def test_shard_typical_flow():
    assert can_transition(ShardStatus.PENDING, ShardStatus.DISPATCHING)
    assert can_transition(ShardStatus.DISPATCHING, ShardStatus.RUNNING)
    assert can_transition(ShardStatus.RUNNING, ShardStatus.SUCCEEDED)
    assert can_transition(ShardStatus.RUNNING, ShardStatus.FAILED)
    assert can_transition(ShardStatus.RUNNING, ShardStatus.WAITING_RECOVERY)
    # 恢复后重新派发或继续
    assert can_transition(ShardStatus.WAITING_RECOVERY, ShardStatus.DISPATCHING)
    assert can_transition(ShardStatus.WAITING_RECOVERY, ShardStatus.RUNNING)
    assert not can_transition(ShardStatus.PENDING, ShardStatus.SUCCEEDED)


def test_attempt_typical_flow():
    assert can_transition(ShardAttemptStatus.CREATED, ShardAttemptStatus.DISPATCHED)
    assert can_transition(ShardAttemptStatus.DISPATCHED, ShardAttemptStatus.ACKED)
    assert can_transition(ShardAttemptStatus.ACKED, ShardAttemptStatus.RUNNING)
    assert can_transition(ShardAttemptStatus.RUNNING, ShardAttemptStatus.SUCCEEDED)
    assert can_transition(ShardAttemptStatus.RUNNING, ShardAttemptStatus.FAILED)
    assert can_transition(ShardAttemptStatus.RUNNING, ShardAttemptStatus.TIMED_OUT)
    assert not can_transition(ShardAttemptStatus.CREATED, ShardAttemptStatus.RUNNING)


# ---------------------------------------------------------------------------
# D-20 三层重试语义（状态机层面）
# ---------------------------------------------------------------------------


def test_failover_new_attempt_semantics():
    """failover：attempt 到 failed 终态后不迁移；新建 attempt_no+1 记录（历史保留）。"""
    assert is_terminal(ShardAttemptStatus.FAILED)
    assert not can_transition(ShardAttemptStatus.FAILED, ShardAttemptStatus.RUNNING)
    assert not can_transition(ShardAttemptStatus.FAILED, ShardAttemptStatus.SUCCEEDED)
    # 新 attempt 序号递增（同一 Shard 换节点重试）
    assert next_attempt_no([]) == 1
    assert next_attempt_no([1]) == 2
    assert next_attempt_no([1, 2]) == 3
    assert next_attempt_no([2, 1]) == 3  # 顺序无关


def test_retry_new_run_semantics():
    """retry（用户/系统重试）= 新建 Run：旧 Run 终态不迁移，新 Run 从 created 开始。"""
    assert is_terminal(RunStatus.FAILED)
    assert not can_transition(RunStatus.FAILED, RunStatus.CREATED)
    assert can_transition(RunStatus.CREATED, RunStatus.DISPATCHED)


def test_run_cancel_projected_from_shards():
    """Run 无 cancelling 中间态：取消由 Shard 投影（所有 active shard 终态后 Run 到终态）。"""
    assert can_transition(RunStatus.RUNNING, RunStatus.CANCELLED)
    # Shard 层面取消是 RUNNING → CANCELLED
    assert can_transition(ShardStatus.RUNNING, ShardStatus.CANCELLED)
    # Attempt 层面取消是 RUNNING → CANCELLED
    assert can_transition(ShardAttemptStatus.RUNNING, ShardAttemptStatus.CANCELLED)


def test_offline_recovery_semantics():
    """节点离线：Attempt 无恢复态，Shard 才有 waiting_recovery（§5.4/§8.7）。"""
    assert not can_transition(ShardAttemptStatus.RUNNING, ShardStatus.WAITING_RECOVERY)
    assert can_transition(ShardStatus.RUNNING, ShardStatus.WAITING_RECOVERY)
    assert not is_terminal(ShardStatus.WAITING_RECOVERY)
