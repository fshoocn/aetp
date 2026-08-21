"""纯函数状态机（P3.6）。

状态迁移由纯函数校验，实体保持纯数据；API/MQTT/前端不得直接改状态
（§5.4）。覆盖四个状态机：

- TaskStatus：任务级（D-22 目标命名，task 执行态）
- RunStatus：Run 总体（created/dispatched/acked/running/.../lost）
- ShardStatus：Run 内分片（含 waiting_recovery 离线恢复等待态）
- ShardAttemptStatus：Shard 向某 Node 的一次派发尝试（D-20）

三层重试语义（D-20）在状态机层面的落点：
- retry（用户/系统）= 新建 Run（新 run_id）
- failover（换节点）= 同 Run 同 Shard 新建 Attempt（attempt_no 递增，
  由 next_attempt_no 辅助）
- case 级重试 = 同 Run 内对该 case 新建 Attempt（run_case_results.attempt_no）
历史失败全量保留：终态（failed）不迁移，只新增下一 attempt 记录。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from master.domain.enums import (
    RunStatus,
    ShardAttemptStatus,
    ShardStatus,
    TaskStatus,
)

_Status = TaskStatus | RunStatus | ShardStatus | ShardAttemptStatus
_StatusT = TypeVar("_StatusT", bound=_Status)


class InvalidStateTransitionError(ValueError):
    """状态迁移非法。"""


# ---------------------------------------------------------------------------
# 迁移表（单一事实来源；frozenset 保证纯函数不可变）
# ---------------------------------------------------------------------------

_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    # pending → dispatching → running → succeeded/failed/timed_out
    # pending → cancelled；dispatching → failed（派发耗尽）；running → cancelling → cancelled
    TaskStatus.PENDING: frozenset({TaskStatus.DISPATCHING, TaskStatus.CANCELLED}),
    TaskStatus.DISPATCHING: frozenset({TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLING,
            TaskStatus.TIMED_OUT,
        }
    ),
    TaskStatus.CANCELLING: frozenset({TaskStatus.CANCELLED}),
}

_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    # created → dispatched → acked → running → succeeded/failed/cancelled/timed_out/lost
    # 无 cancelling 中间态：取消由 Shard 投影（所有 active shard 终态后 Run 到终态，§5.4）
    RunStatus.CREATED: frozenset({RunStatus.DISPATCHED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.DISPATCHED: frozenset(
        {
            RunStatus.ACKED,
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.ACKED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.LOST,
        }
    ),
}

_SHARD_TRANSITIONS: dict[ShardStatus, frozenset[ShardStatus]] = {
    # pending → dispatching → running → succeeded/failed/cancelled/timed_out
    # running → waiting_recovery（节点离线等待恢复）；恢复后重新派发/继续
    ShardStatus.PENDING: frozenset({ShardStatus.DISPATCHING, ShardStatus.FAILED, ShardStatus.CANCELLED}),
    ShardStatus.DISPATCHING: frozenset({ShardStatus.RUNNING, ShardStatus.FAILED, ShardStatus.CANCELLED}),
    ShardStatus.RUNNING: frozenset(
        {
            ShardStatus.SUCCEEDED,
            ShardStatus.FAILED,
            ShardStatus.CANCELLED,
            ShardStatus.TIMED_OUT,
            ShardStatus.WAITING_RECOVERY,
        }
    ),
    ShardStatus.WAITING_RECOVERY: frozenset(
        {
            ShardStatus.RUNNING,
            ShardStatus.DISPATCHING,
            ShardStatus.FAILED,
            ShardStatus.CANCELLED,
            ShardStatus.TIMED_OUT,
        }
    ),
}

_ATTEMPT_TRANSITIONS: dict[ShardAttemptStatus, frozenset[ShardAttemptStatus]] = {
    # created → dispatched → acked → running → succeeded/failed/cancelled/timed_out
    # failover：attempt 到 failed（终态）后不迁移，由调度器新建 attempt_no+1（D-20）
    ShardAttemptStatus.CREATED: frozenset(
        {ShardAttemptStatus.DISPATCHED, ShardAttemptStatus.FAILED, ShardAttemptStatus.CANCELLED}
    ),
    ShardAttemptStatus.DISPATCHED: frozenset(
        {
            ShardAttemptStatus.ACKED,
            ShardAttemptStatus.RUNNING,
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.CANCELLED,
        }
    ),
    ShardAttemptStatus.ACKED: frozenset(
        {ShardAttemptStatus.RUNNING, ShardAttemptStatus.FAILED, ShardAttemptStatus.CANCELLED}
    ),
    ShardAttemptStatus.RUNNING: frozenset(
        {
            ShardAttemptStatus.SUCCEEDED,
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.CANCELLED,
            ShardAttemptStatus.TIMED_OUT,
        }
    ),
}

# 终态表（终态不可再迁移）
_TERMINAL: dict[type, frozenset] = {
    TaskStatus: frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT}),
    RunStatus: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.LOST,
        }
    ),
    ShardStatus: frozenset({ShardStatus.SUCCEEDED, ShardStatus.FAILED, ShardStatus.CANCELLED, ShardStatus.TIMED_OUT}),
    ShardAttemptStatus: frozenset(
        {
            ShardAttemptStatus.SUCCEEDED,
            ShardAttemptStatus.FAILED,
            ShardAttemptStatus.CANCELLED,
            ShardAttemptStatus.TIMED_OUT,
        }
    ),
}

# 状态枚举 → 迁移表
_TRANSITIONS: dict[type, dict] = {
    TaskStatus: _TASK_TRANSITIONS,
    RunStatus: _RUN_TRANSITIONS,
    ShardStatus: _SHARD_TRANSITIONS,
    ShardAttemptStatus: _ATTEMPT_TRANSITIONS,
}


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def transitions_for(status: _StatusT) -> frozenset[_StatusT]:
    """返回某状态可合法迁移到的目标状态集合。"""
    table = _TRANSITIONS[type(status)]
    return frozenset(table.get(status, frozenset()))


def can_transition(status: _StatusT, target: _StatusT) -> bool:
    """纯函数：判断 status -> target 是否为合法迁移。"""
    if type(status) is not type(target):
        return False
    return target in transitions_for(status)


def assert_transition(status: _StatusT, target: _StatusT) -> None:
    """校验迁移合法性，非法抛出 InvalidStateTransitionError。"""
    if not can_transition(status, target):
        raise InvalidStateTransitionError(f"非法状态迁移: {type(status).__name__} {status.value} -> {target.value}")


def is_terminal(status: _StatusT) -> bool:
    """判断状态是否为终态（终态不可再迁移）。"""
    return status in _TERMINAL[type(status)]


def next_attempt_no(existing: Iterable[int]) -> int:
    """D-20 failover：返回同 Shard 下一个 attempt 序号（现有最大 + 1，默认 1）。"""
    seqs = list(existing)
    return (max(seqs) + 1) if seqs else 1
