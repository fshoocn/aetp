"""状态机转移表、任务规格校验与错误类型再导出（DESIGN §5）。

状态字面量、契约数据结构与错误类型在公共层 :mod:`common.test_contracts`，
本模块再导出供同包与测试使用；迁移表与校验都是纯函数，不依赖 db / cordis。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Literal

from common.test_contracts import (
    InvalidSpec,
    InvalidTransition,
    JobState,
    TaskNotFound,
    TaskSpec,
    TaskState,
    TestCoreError,
)

#: Task 状态迁移表（DESIGN §5.1）：终态无出边；failed → running 仅供 rerun_failed 复活
TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"planning", "running", "canceling"}),
    "planning": frozenset({"running", "canceling", "error"}),
    "running": frozenset({"succeeded", "failed", "error", "canceling"}),
    "canceling": frozenset({"canceled", "error"}),
    "failed": frozenset({"running"}),
    "succeeded": frozenset(),
    "canceled": frozenset(),
    "error": frozenset(),
}

#: Task 终态（无出边；failed 不在此列——仅供 rerun_failed 复活）
TASK_TERMINAL: frozenset[str] = frozenset({"succeeded", "canceled", "error"})

#: Task 已终结集合（含 failed）：cancel / set_priority 等操作的拒绝口径；
#: failed 唯一的复活通道是 rerun_failed（failed → running）
TASK_FINISHED: frozenset[str] = frozenset({"succeeded", "failed", "canceled", "error"})

#: Job 状态迁移表（DESIGN §5.2）：lost 可重入队或判失败；终态无出边
JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"leased", "canceled"}),
    "leased": frozenset({"running", "succeeded", "failed", "canceled", "lost"}),
    "running": frozenset({"succeeded", "failed", "canceled", "lost"}),
    "lost": frozenset({"queued", "failed"}),
    "failed": frozenset({"queued"}),  # 仅供 rerun_failed 复活（同 Job 行 attempt+1）
    "succeeded": frozenset(),
    "canceled": frozenset(),
}

#: Job 终态（无出边；failed / lost 不在此列——可复活或重入队）
JOB_TERMINAL: frozenset[str] = frozenset({"succeeded", "canceled"})

#: Job 已完结集合（Stage 屏障口径）：failed 算完结，复活通道仅 rerun_failed
JOB_FINISHED: frozenset[str] = frozenset({"succeeded", "failed", "canceled"})


def _table_for(kind: Literal["task", "job"]) -> dict[str, frozenset[str]]:
    """按种类取迁移表；未知种类是编码错误（ValueError）。"""
    if kind == "task":
        return TASK_TRANSITIONS
    if kind == "job":
        return JOB_TRANSITIONS
    raise ValueError(f"未知状态机种类 {kind!r}，可选：task / job")


def can_transition(kind: Literal["task", "job"], current: str, target: str) -> bool:
    """迁移是否合法：未知状态 / 无出边 / 未知目标一律 False。"""
    targets = _table_for(kind).get(current)
    return targets is not None and target in targets


def transition(kind: Literal["task", "job"], current: str, target: str, *, reason: str = "") -> str:
    """校验并完成一次状态迁移：合法返回 target，非法抛 InvalidTransition。"""
    if not can_transition(kind, current, target):
        detail = f"，原因：{reason}" if reason else ""
        raise InvalidTransition(f"非法 {kind} 状态迁移：{current!r} → {target!r}{detail}")
    return target


def _timeout_problem(path: str, raw: object) -> str | None:
    """timeout_s 需是有限非负数（0 = 不限）；返回问题文本，合法返回 None。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return f"{path} 需是有限非负数（0=不限），收到 {raw!r}"
    try:
        value = float(raw)
    except OverflowError:
        return f"{path} 需是有限非负数（0=不限），收到 {raw!r}"
    if not math.isfinite(value) or value < 0:
        return f"{path} 需是有限非负数（0=不限），收到 {raw!r}"
    return None


def validate_task_spec(spec: TaskSpec) -> list[str]:
    """校验任务规格（DESIGN §3.1），返回问题清单（空清单 = 合法）；纯函数、不抛。"""
    problems: list[str] = []
    if not isinstance(spec.name, str) or not spec.name.strip():
        problems.append(f"name 需是非空字符串，收到 {spec.name!r}")
    priority = spec.priority
    if isinstance(priority, bool) or not isinstance(priority, int):
        problems.append(f"priority 需是 int，收到 {priority!r}")
    if spec.failure_policy not in ("fail_fast", "continue"):
        problems.append(f"failure_policy 非法，收到 {spec.failure_policy!r}（可选：fail_fast / continue）")
    problem = _timeout_problem("timeout_s", spec.timeout_s)
    if problem is not None:
        problems.append(problem)

    stages = list(spec.stages or ())
    if not stages:
        problems.append("stages 不能为空")
        return problems
    seen: set[str] = set()
    for index, stage in enumerate(stages):
        where = f"stages[{index}]"
        stage_name = getattr(stage, "name", None)
        if not isinstance(stage_name, str) or not stage_name.strip():
            problems.append(f"{where}.name 需是非空字符串")
        elif stage_name in seen:
            problems.append(f"{where}.name 重复：{stage_name!r}")
        else:
            seen.add(stage_name)
        executor = getattr(stage, "executor", None)
        executor_id = getattr(executor, "id", None)
        if not isinstance(executor_id, str) or not executor_id.strip():
            problems.append(f"{where}.executor.id 需是非空字符串")
        # 台架定向（编辑器 v2 设计 §4/§8.5）：getattr 宽松——缺席按「不指定」放行
        nodes = getattr(executor, "nodes", None)
        if nodes is not None:
            if isinstance(nodes, (str, bytes)) or not isinstance(nodes, Sequence):
                problems.append(f"{where}.executor.nodes 需是字符串数组，收到 {nodes!r}")
            else:
                for item in nodes:
                    if not isinstance(item, str):
                        problems.append(f"{where}.executor.nodes 含非字符串项：{item!r}")
                    elif not item:
                        problems.append(f"{where}.executor.nodes 含空串（台架名需非空）")
        strategy = getattr(executor, "strategy", None)
        if strategy is not None and strategy not in ("balanced", "random"):
            problems.append(
                f"{where}.executor.strategy 非法，收到 {strategy!r}（可选：balanced / random）"
            )
        problem = _timeout_problem(f"{where}.timeout_s", getattr(stage, "timeout_s", None))
        if problem is not None:
            problems.append(problem)
        mode = getattr(getattr(stage, "shard", None), "mode", None)
        if mode is not None and mode not in ("none", "plan"):
            problems.append(f"{where}.shard.mode 非法，收到 {mode!r}（可选：none / plan）")
        hint = getattr(getattr(stage, "shard", None), "hint", None)
        if isinstance(hint, bool) or not isinstance(hint, int) or hint < 0:
            problems.append(f"{where}.shard.hint 需是非负 int，收到 {hint!r}")
    return problems


def ensure_valid(spec: TaskSpec) -> None:
    """规格有问题时整体抛 InvalidSpec（消息含全部问题）；合法即静默返回。"""
    problems = validate_task_spec(spec)
    if problems:
        raise InvalidSpec("任务规格无效：" + "；".join(problems))


def history_row(task_id: str, from_state: str, to_state: str, reason: str, ts: float, row_id: str) -> dict[str, Any]:
    """构造 task_history 行字典（纯函数，键同 :class:`TestTaskHistory` 列）。"""
    return {
        "id": row_id,
        "task_id": task_id,
        "from_state": from_state,
        "to_state": to_state,
        "reason": reason,
        "ts": ts,
    }


__all__: list[str] = [
    "JOB_FINISHED",
    "JOB_TERMINAL",
    "JOB_TRANSITIONS",
    "TASK_FINISHED",
    "TASK_TERMINAL",
    "TASK_TRANSITIONS",
    "InvalidSpec",
    "InvalidTransition",
    "JobState",
    "TaskNotFound",
    "TaskSpec",
    "TaskState",
    "TestCoreError",
    "can_transition",
    "ensure_valid",
    "history_row",
    "transition",
    "validate_task_spec",
]
