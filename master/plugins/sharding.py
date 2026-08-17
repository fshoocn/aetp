"""Master 侧 Shard 分割纯函数（P6.3，§18.6，D-21）。

插件 ``split_shards`` 的 by_time / by_case_count / custom 策略，其
「把用例列表切成 ShardSpec」的通用算法收敛到本模块，插件只需传入用例
与策略参数，无需各自重写装箱逻辑。

- ``split_by_case_count``：每 N 个 case 一个 Shard；
- ``split_by_time``：按 case 耗时切到目标时长（依赖 ``estimated_duration_s``，
  D-21：缺耗时的 case 按可配置默认值参与切片，不因缺数据失败）；
- ``split_none``：不分割，单 Shard。

``execution_params`` 由调用方按需注入（每 Shard 专属执行参数，如 CAN 通道）。
本模块只依赖共享 ``CaseInfo`` / ``ShardSpec``，不接触数据库/MQTT/FastAPI。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from aetp_protocol.plugin import CaseInfo, ShardSpec


class SplitPolicyError(ValueError):
    """分割策略参数非法。"""


def _split_into_bins(
    cases: list[CaseInfo],
    *,
    bin_size: Callable[[CaseInfo], float],
    capacity: float,
    default_duration_s: float,
) -> list[list[CaseInfo]]:
    """按「每个 case 的占位大小」装箱，超过 capacity 换下一个 bin。

    ``bin_size`` 返回每个 case 占用的容量；``capacity`` 为单 bin 上限。
    任何 case 的 ``bin_size`` 非正数时，用 ``default_duration_s`` 兜底
    （D-21：缺耗时的 case 按可配置默认值参与切片，不因缺数据失败）。
    """
    if capacity <= 0:
        raise SplitPolicyError(f"目标时长必须大于 0: {capacity}")
    if default_duration_s <= 0:
        raise SplitPolicyError(f"缺耗时默认值必须大于 0: {default_duration_s}")
    if not cases:
        return []

    bins: list[list[CaseInfo]] = []
    current: list[CaseInfo] = []
    current_size = 0.0
    for case in cases:
        size = bin_size(case)
        if size is None or size <= 0:
            size = default_duration_s
        if current and current_size + size > capacity:
            bins.append(current)
            current = []
            current_size = 0.0
        current.append(case)
        current_size += size
    if current:
        bins.append(current)
    return bins


def split_none(cases: list[CaseInfo], **_kwargs: Any) -> list[ShardSpec]:
    """不分割：所有 case 归入单个 Shard。"""
    return [
        ShardSpec(
            case_keys=tuple(case.stable_key for case in cases),
            estimated_duration_s=_total_duration(cases),
        )
    ]


def split_by_case_count(
    cases: list[CaseInfo],
    *,
    cases_per_shard: int,
    **_: Any,
) -> list[ShardSpec]:
    """每 ``cases_per_shard`` 个 case 一个 Shard。"""
    if cases_per_shard <= 0:
        raise SplitPolicyError(f"cases_per_shard 必须大于 0: {cases_per_shard}")
    shards = [
        cases[index : index + cases_per_shard]
        for index in range(0, len(cases), cases_per_shard)
    ]
    return [_to_shard(bin_) for bin_ in shards]


def split_by_time(
    cases: list[CaseInfo],
    *,
    target_duration_s: float,
    default_duration_s: float = 60.0,
    **_: Any,
) -> list[ShardSpec]:
    """按 case 耗时切到目标时长；缺耗时 case 用默认值参与切片（D-21）。"""
    bins = _split_into_bins(
        cases,
        bin_size=lambda case: case.estimated_duration_s,
        capacity=target_duration_s,
        default_duration_s=default_duration_s,
    )
    return [_to_shard(bin_) for bin_ in bins]


def _total_duration(cases: list[CaseInfo]) -> float | None:
    """Sum estimated durations; None if any case lacks it. """
    total = 0.0
    for case in cases:
        if case.estimated_duration_s is None:
            return None
        total += case.estimated_duration_s
    return total


def _to_shard(cases: list[CaseInfo]) -> ShardSpec:
    return ShardSpec(
        case_keys=tuple(case.stable_key for case in cases),
        estimated_duration_s=_total_duration(cases),
    )
