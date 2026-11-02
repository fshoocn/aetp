"""步骤解析（§5.6）：节点选择器 → 提供者列表（含歧义判定）。

预检与运行期共用同一套解析（晚绑定的「设计期下拉与运行期一致」）：

* 只看 ``alive`` 的注册项（提供者 fiber ACTIVE 且未排空）；
* 选择器 = ``stage`` + ``labels``（对信封标签做**子集匹配**）+ 可选 ``pin``；
* 排序键 ``(-priority, provider, step_id)``——确定性、与注册顺序无关；
* ``mode=one`` 且头两名同优先级且未 ``pin`` → **歧义**（不「碰巧」选第一个）。
"""

from __future__ import annotations

from dataclasses import dataclass

from common.flow_contracts import Mode, NodeDef, Problem, ProblemCode, ProblemLevel

from .registry import StepRecord


@dataclass(frozen=True, slots=True)
class Resolution:
    """一次解析的结果：选中项 + 可能的问题 + 全部候选（报错时列近似项）。"""

    picked: tuple[StepRecord, ...]
    problem: Problem | None
    candidates: tuple[StepRecord, ...]


def _matches(record: StepRecord, node: NodeDef) -> bool:
    labels = record.selector_labels()
    wanted = {"stage": node.stage, **dict(node.labels)}
    if not all(labels.get(key) == value for key, value in wanted.items()):
        return False
    if node.pin is not None and record.step_id != node.pin:
        return False
    return True


def _sort_key(record: StepRecord) -> tuple[int, str, str]:
    return (-record.priority, record.provider, record.step_id)


def resolve_node(records: list[StepRecord], node: NodeDef) -> Resolution:
    """按选择器解析一个节点的提供者（§5.6 的 ``resolve``）。"""
    alive = [record for record in records if record.alive]
    candidates = sorted((r for r in alive if _matches(r, node)), key=_sort_key)
    if not candidates:
        return Resolution(
            (),
            Problem(
                ProblemLevel.ERROR if node.required else ProblemLevel.WARN,
                node.id,
                ProblemCode.NO_PROVIDER,
                f"stage={node.stage} labels={dict(node.labels)}"
                f"{' pin=' + node.pin if node.pin else ''} 无可用提供者",
            ),
            (),
        )
    if node.mode is Mode.ALL:
        return Resolution(tuple(candidates), None, tuple(candidates))
    if len(candidates) > 1 and node.pin is None:
        first, second = candidates[0], candidates[1]
        if first.priority == second.priority:
            return Resolution(
                (),
                Problem(
                    ProblemLevel.ERROR,
                    node.id,
                    ProblemCode.AMBIGUOUS,
                    f"stage={node.stage} 候选并列（{first.provider}/{first.step_id} 与 "
                    f"{second.provider}/{second.step_id}）：加 labels / pin / priority 消歧",
                ),
                tuple(candidates),
            )
    return Resolution((candidates[0],), None, tuple(candidates))


def catalog_line(record: StepRecord) -> str:
    """预检报错时的「已注册步骤」清单条目（如 ``report/html``）。"""
    return f"{record.stage}/{record.step_id}"


def no_provider_problem(node: NodeDef, records: list[StepRecord]) -> Problem:
    """缺提供者的问题描述：列出当前已注册的步骤与近似项（§5.7）。"""
    registered = ", ".join(sorted(catalog_line(r) for r in records if r.alive)) or "（空）"
    near = [
        catalog_line(record)
        for record in records
        if record.alive and record.stage == node.stage and record.step_id != node.pin
    ]
    hint = f"；近似项：{', '.join(sorted(set(near)))}" if near else ""
    return Problem(
        ProblemLevel.ERROR if node.required else ProblemLevel.WARN,
        node.id,
        ProblemCode.NO_PROVIDER,
        f"stage={node.stage} labels={dict(node.labels)}；已注册：{registered}{hint}",
    )


__all__: list[str] = ["Resolution", "catalog_line", "no_provider_problem", "resolve_node"]
