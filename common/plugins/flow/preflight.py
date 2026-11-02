"""预检与覆盖应用（§5.7 / §5.14.2）：模板 + ``Overrides`` → 生效节点 + 问题清单。

覆盖语义（用户流程与单次运行覆盖同构）：

* ``disable``：只允许可选且非 ``locked`` 的节点（``locked`` / 必选 → 拒绝）；
* ``insert``：只进模板声明的**插槽**（``after`` 存在且阶段在插槽允许内），
  新节点缺省为**可选**；
* ``params`` / ``pin`` / ``labels``：目标须存在、非 ``locked``；``params``
  须符合提供者的 ``params_schema``；
* 重排节点 v1 不支持（用「关闭 + 插入」替代）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from common.flow_contracts import (
    FlowDef,
    JsonValue,
    Mode,
    NodeDef,
    Overrides,
    Problem,
    ProblemCode,
    ProblemLevel,
    SlotDef,
)

from .registry import StepRecord
from .resolver import no_provider_problem, resolve_node

#: 节点 id 语法（§5.5.1）
_NODE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
#: 单节点 params 尺寸上限（§5.5.3）
_MAX_PARAMS_BYTES = 16 * 1024
#: 模板规模上限（§5.5.3）
_MAX_NODES = 64
_MAX_LABELS = 16


def validate_flow_def(flow: FlowDef) -> None:
    """模板形状校验（``define()`` 时执行；不合即 ``ValueError``，快速暴露）。"""
    if not _NODE_ID.fullmatch(flow.id):
        raise ValueError(f"流程 id 不合语法 [a-z0-9][a-z0-9_-]{{0,39}}：{flow.id!r}")
    if len(flow.nodes) > _MAX_NODES:
        raise ValueError(f"流程节点数超上限 {_MAX_NODES}：{len(flow.nodes)}")
    seen: set[str] = set()
    for node in flow.nodes:
        if not _NODE_ID.fullmatch(node.id):
            raise ValueError(f"节点 id 不合语法 [a-z0-9][a-z0-9_-]{{0,39}}：{node.id!r}")
        if not node.stage:
            raise ValueError(f"节点 {node.id!r} 的 stage 为空")
        if node.id in seen:
            raise ValueError(f"节点 id 重复：{node.id!r}")
        seen.add(node.id)
        if len(node.labels) > _MAX_LABELS:
            raise ValueError(f"节点 {node.id!r} 的 labels 超上限 {_MAX_LABELS}")
        if len(json.dumps(dict(node.params), ensure_ascii=False)) > _MAX_PARAMS_BYTES:
            raise ValueError(f"节点 {node.id!r} 的 params 超上限 {_MAX_PARAMS_BYTES} 字节")


def _check_value(name: str, spec: object, value: JsonValue, errors: list[str]) -> None:
    """按 JSON Schema 子集校验单个值（type / enum / minimum / maximum /
    pattern / maxLength / items）；错误追加进 ``errors``。"""
    if not isinstance(spec, dict):
        return
    spec_d = dict(spec)
    kind = spec_d.get("type")
    if kind == "string":
        if not isinstance(value, str):
            errors.append(f"{name} 需是 string，收到 {type(value).__name__}")
            return
        max_length = spec_d.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{name} 超长（maxLength={max_length}）")
        pattern = spec_d.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{name} 不匹配 pattern={pattern!r}")
    elif kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{name} 需是 {kind}，收到 {type(value).__name__}")
            return
        if kind == "integer" and not isinstance(value, int):
            errors.append(f"{name} 需是 integer，收到 float")
        minimum = spec_d.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{name} 小于 minimum={minimum}")
        maximum = spec_d.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{name} 大于 maximum={maximum}")
    elif kind == "boolean" and not isinstance(value, bool):
        errors.append(f"{name} 需是 boolean，收到 {type(value).__name__}")
    elif kind == "array":
        if not isinstance(value, list):
            errors.append(f"{name} 需是 array，收到 {type(value).__name__}")
            return
        items = spec_d.get("items")
        for index, item in enumerate(value):
            _check_value(f"{name}[{index}]", items, item, errors)
    elif kind == "object" and not isinstance(value, dict):
        errors.append(f"{name} 需是 object，收到 {type(value).__name__}")
    enum = spec_d.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{name} 不在 enum={enum} 内")


def validate_params(
    schema: Mapping[str, JsonValue] | None, params: Mapping[str, JsonValue]
) -> list[str]:
    """参数校验（§5.3 的 ``params_schema`` 子集）；返回错误消息列表。"""
    if not schema:
        return []
    errors: list[str] = []
    required_raw = schema.get("required")
    if isinstance(required_raw, list):
        for key in required_raw:
            if isinstance(key, str) and key not in params:
                errors.append(f"缺必填参数 {key!r}")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, value in params.items():
            spec = properties.get(key)
            if spec is not None:
                _check_value(f"参数 {key!r}", spec, value, errors)
    return errors


def _parse_node(raw: Mapping[str, JsonValue], *, default_required: bool) -> NodeDef:
    """把节点的 JSON 形态解析成 :class:`NodeDef`（附录 C.2 同构）。"""
    node_id = raw.get("id")
    stage = raw.get("stage")
    if not isinstance(node_id, str) or not isinstance(stage, str):
        raise TypeError("节点必须提供字符串 id 与 stage")
    labels_raw = raw.get("labels")
    params_raw = raw.get("params")
    mode_raw = raw.get("mode")
    timeout_raw = raw.get("timeout")
    retries_raw = raw.get("retries")
    backoff_raw = raw.get("backoff")
    return NodeDef(
        id=node_id,
        stage=stage,
        labels=(
            {str(k): str(v) for k, v in dict(labels_raw).items()}
            if isinstance(labels_raw, dict)
            else {}
        ),
        pin=raw.get("pin") if isinstance(raw.get("pin"), str) else None,
        mode=Mode(mode_raw) if mode_raw in ("one", "all") else Mode.ONE,
        required=bool(raw.get("required", default_required)),
        locked=bool(raw.get("locked", False)),
        always=bool(raw.get("always", False)),
        params=dict(params_raw) if isinstance(params_raw, dict) else {},
        retries=int(retries_raw) if isinstance(retries_raw, int) else 0,
        backoff=float(backoff_raw) if isinstance(backoff_raw, (int, float)) else 1.0,
        timeout=float(timeout_raw) if isinstance(timeout_raw, (int, float)) else None,
    )


def _parse_insert_node(raw: Mapping[str, JsonValue]) -> NodeDef:
    """把插入节点的 JSON 形态解析成 :class:`NodeDef`（新节点缺省可选）。"""
    return _parse_node(raw, default_required=False)


def parse_flow_def(raw: Mapping[str, JsonValue]) -> FlowDef:
    """JSON 形态（``$schema: aetp.flow/1``，附录 C.2 同构）→ :class:`FlowDef`。

    完整定义的节点 ``required`` 缺省为 ``True``（与代码形态一致）；结构不合
    抛 ``TypeError`` / ``ValueError``（保存接口映射 400）。
    """
    flow_id = raw.get("id")
    nodes_raw = raw.get("nodes")
    if not isinstance(flow_id, str) or not isinstance(nodes_raw, list):
        raise TypeError("流程必须提供字符串 id 与 nodes 列表")
    parsed_nodes: list[NodeDef] = []
    for item in nodes_raw:
        if not isinstance(item, dict):
            raise TypeError("nodes 条目必须是对象")
        parsed_nodes.append(_parse_node(dict(item), default_required=True))
    nodes = tuple(parsed_nodes)
    slots_raw = raw.get("slots") or []
    if not isinstance(slots_raw, list):
        raise TypeError("slots 必须是列表")
    slots: list[SlotDef] = []
    for item in slots_raw:
        if not isinstance(item, dict):
            raise TypeError("slots 条目必须是对象")
        slot_id = item.get("id")
        after = item.get("after")
        stages = item.get("stages")
        if not isinstance(slot_id, str) or not isinstance(after, str) or not isinstance(stages, list):
            raise TypeError("插槽必须提供字符串 id / after 与 stages 列表")
        if not stages or not all(isinstance(s, str) for s in stages):
            raise ValueError(f"插槽 {slot_id!r} 的 stages 须是非空字符串列表")
        slots.append(SlotDef(id=slot_id, after=after, stages=tuple(str(s) for s in stages)))
    flow = FlowDef(
        id=flow_id,
        version=int(raw.get("version") or 1),
        nodes=nodes,
        slots=tuple(slots),
        title=str(raw.get("title") or ""),
    )
    validate_flow_def(flow)
    node_ids = {node.id for node in flow.nodes}
    for slot in flow.slots:
        if slot.after not in node_ids:
            raise ValueError(f"插槽 {slot.id!r} 的 after={slot.after!r} 不是已知节点")
    return flow


def flow_def_to_json(flow: FlowDef) -> dict[str, JsonValue]:
    """FlowDef → JSON 形态（导出 / 详情用；与 :func:`parse_flow_def` 互逆）。"""
    return {
        "id": flow.id,
        "version": flow.version,
        "title": flow.title,
        "nodes": [
            {
                "id": node.id,
                "stage": node.stage,
                "labels": dict(node.labels),
                "pin": node.pin,
                "mode": node.mode.value,
                "required": node.required,
                "locked": node.locked,
                "always": node.always,
                "params": dict(node.params),
                "retries": node.retries,
                "backoff": node.backoff,
                "timeout": node.timeout,
            }
            for node in flow.nodes
        ],
        "slots": [
            {"id": slot.id, "after": slot.after, "stages": list(slot.stages)}
            for slot in flow.slots
        ],
    }


def _merge_node(node: NodeDef, overrides: Overrides) -> NodeDef:
    """把 params / pin / labels 覆盖合并进模板节点（其余字段不动）。"""
    params = dict(node.params)
    params.update(dict(overrides.get("params", {}).get(node.id, {})))
    labels = dict(node.labels)
    labels.update(dict(overrides.get("labels", {}).get(node.id, {})))
    pin = overrides.get("pin", {}).get(node.id, node.pin)
    return NodeDef(
        id=node.id,
        stage=node.stage,
        labels=labels,
        pin=pin,
        mode=node.mode,
        required=node.required,
        locked=node.locked,
        always=node.always,
        params=params,
        retries=node.retries,
        backoff=node.backoff,
        timeout=node.timeout,
    )


def apply_overrides(
    flow: FlowDef, overrides: Overrides | None
) -> tuple[list[NodeDef], set[str], list[Problem]]:
    """模板 + 覆盖 → (生效节点列表, 被关闭的节点 id, 问题)。

    被关闭的节点**留在列表里**（执行时记 ``skipped(disabled)``,时间线完整），
    插入节点跟在其 ``after`` 之后；``disable`` 也可指向**本次插入的节点**
    （「关闭 + 插入」组合，叠加覆盖时后者关前者插的）。
    """
    problems: list[Problem] = []
    if not overrides:
        return list(flow.nodes), set(), problems
    by_id = {node.id: node for node in flow.nodes}
    slots_by_after: dict[str, list[SlotDef]] = {}
    for slot in flow.slots:
        slots_by_after.setdefault(slot.after, []).append(slot)

    # 插入先解析（disable / params 等可能指向插入节点）
    inserted: dict[str, list[NodeDef]] = {}
    inserted_ids: set[str] = set()
    for spec in overrides.get("insert", []):
        after = spec.get("after")
        raw = spec.get("node")
        if not isinstance(after, str) or not isinstance(raw, dict):
            problems.append(
                Problem(ProblemLevel.ERROR, "", ProblemCode.UNKNOWN_SLOT,
                        "insert 条目必须是 {after, node} 且 node 为对象")
            )
            continue
        try:
            new_node = _parse_insert_node(dict(raw))
        except (TypeError, ValueError) as exc:
            problems.append(
                Problem(ProblemLevel.ERROR, "", ProblemCode.UNKNOWN_SLOT, str(exc))
            )
            continue
        if after not in by_id:
            problems.append(
                Problem(ProblemLevel.ERROR, new_node.id, ProblemCode.UNKNOWN_SLOT,
                        f"after={after!r} 不是模板节点")
            )
            continue
        slots = slots_by_after.get(after, [])
        if not any(new_node.stage in slot.stages for slot in slots):
            problems.append(
                Problem(ProblemLevel.ERROR, new_node.id, ProblemCode.UNKNOWN_SLOT,
                        f"插槽 {after!r} 不允许阶段 {new_node.stage!r}"
                        f"（允许：{sorted({s for slot in slots for s in slot.stages})}）")
            )
            continue
        if new_node.id in by_id or new_node.id in inserted_ids:
            problems.append(
                Problem(ProblemLevel.ERROR, new_node.id, ProblemCode.DUPLICATE_NODE,
                        f"插入节点 id 与现有节点重复：{new_node.id!r}")
            )
            continue
        inserted.setdefault(after, []).append(new_node)
        inserted_ids.add(new_node.id)

    disabled: set[str] = set()
    for target in overrides.get("disable", []):
        node = by_id.get(target)
        if node is not None:
            if node.locked or node.required:
                problems.append(
                    Problem(ProblemLevel.ERROR, target, ProblemCode.LOCKED,
                            f"节点 {target!r} 是{'锁定' if node.locked else '必选'}节点，不可关闭")
                )
            else:
                disabled.add(target)
        elif target in inserted_ids:
            disabled.add(target)  # 关闭插入节点（缺省可选，允许关）
        else:
            problems.append(
                Problem(ProblemLevel.ERROR, target, ProblemCode.UNKNOWN_NODE,
                        f"disable 指向不存在的节点 {target!r}")
            )

    for field, targets in (
        ("params", overrides.get("params", {})),
        ("pin", overrides.get("pin", {})),
        ("labels", overrides.get("labels", {})),
    ):
        for target in targets:
            node = by_id.get(target)
            if node is None and target not in inserted_ids:
                problems.append(
                    Problem(ProblemLevel.ERROR, target, ProblemCode.UNKNOWN_NODE,
                            f"{field} 指向不存在的节点 {target!r}")
                )
            elif node is not None and node.locked:
                problems.append(
                    Problem(ProblemLevel.ERROR, target, ProblemCode.LOCKED,
                            f"节点 {target!r} 已锁定，不可修改")
                )

    for after in inserted:
        if after in disabled:
            problems.append(
                Problem(ProblemLevel.ERROR, "", ProblemCode.UNKNOWN_SLOT,
                        f"插入锚点 {after!r} 已被关闭")
            )

    result: list[NodeDef] = []
    for node in flow.nodes:
        result.append(_merge_node(node, overrides))
        for new_node in inserted.get(node.id, []):
            # 关闭的插入节点留在列表里（时间线记 skipped(disabled)，与模板节点一致）
            result.append(_merge_node(new_node, overrides))
    return result, disabled, problems


def preflight_flow(
    flow: FlowDef, overrides: Overrides | None, records: list[StepRecord]
) -> list[Problem]:
    """预检（§5.7）：结构 + 覆盖 + 当前能力（缺提供者 / 歧义 / 参数）。"""
    nodes, disabled, problems = apply_overrides(flow, overrides)
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            problems.append(
                Problem(ProblemLevel.ERROR, node.id, ProblemCode.DUPLICATE_NODE,
                        f"节点 id 重复：{node.id!r}")
            )
        seen.add(node.id)
    for node in nodes:
        if node.id in disabled:
            continue  # 已关闭的节点不再解析
        resolution = resolve_node(records, node)
        problem = resolution.problem
        if problem is not None and problem.code is ProblemCode.NO_PROVIDER:
            problem = no_provider_problem(node, records)  # 消息里列已注册步骤与近似项
        if problem is not None:
            problems.append(problem)
            continue
        if node.params:
            for record in resolution.picked:
                for message in validate_params(record.params_schema, node.params):
                    problems.append(
                        Problem(ProblemLevel.ERROR, node.id, ProblemCode.BAD_PARAMS, message)
                    )
    return problems


__all__: list[str] = [
    "apply_overrides",
    "flow_def_to_json",
    "parse_flow_def",
    "preflight_flow",
    "validate_flow_def",
    "validate_params",
]
