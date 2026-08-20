"""P6.3：Master 侧 Shard 分割策略契约测试（by_time/by_case_count/none，D-21）。"""

from __future__ import annotations

import pytest
from aetp_protocol.plugin import CaseInfo

from master.plugins.sharding import (
    SplitPolicyError,
    split_by_case_count,
    split_by_time,
    split_none,
)


def _case(key: str, duration: float | None) -> CaseInfo:
    return CaseInfo(stable_key=key, name=key, estimated_duration_s=duration)


# -----------------------------------------------------------------------
# none（不分割）
# -----------------------------------------------------------------------

def test_split_none_single_shard() -> None:
    cases = [_case("a", 1), _case("b", 2)]
    shards = split_none(cases)
    assert len(shards) == 1
    assert shards[0].case_keys == ("a", "b")
    assert shards[0].estimated_duration_s == 3


# -----------------------------------------------------------------------
# by_case_count
# -----------------------------------------------------------------------

def test_split_by_case_count() -> None:
    cases = [_case("a", 1), _case("b", 2), _case("c", 3), _case("d", 4), _case("e", 5)]
    shards = split_by_case_count(cases, cases_per_shard=2)
    assert [s.case_keys for s in shards] == [("a", "b"), ("c", "d"), ("e",)]
    # 每个 Shard 保留自己的预估耗时
    assert shards[0].estimated_duration_s == 3
    assert shards[1].estimated_duration_s == 7
    assert shards[2].estimated_duration_s == 5


def test_split_by_case_count_rejects_non_positive() -> None:
    with pytest.raises(SplitPolicyError):
        split_by_case_count([_case("a", 1)], cases_per_shard=0)


# -----------------------------------------------------------------------
# by_time
# -----------------------------------------------------------------------

def test_split_by_time_balances_by_duration() -> None:
    cases = [
        _case("a", 30),
        _case("b", 30),
        _case("c", 30),
        _case("d", 30),
    ]
    shards = split_by_time(cases, target_duration_s=60, default_duration_s=60)
    assert [s.case_keys for s in shards] == [("a", "b"), ("c", "d")]
    assert shards[0].estimated_duration_s == 60
    assert shards[1].estimated_duration_s == 60


def test_split_by_time_uses_default_when_missing_duration() -> None:
    """D-21：缺耗时的 case 按可配置默认值参与切片，不因缺数据失败。"""
    cases = [
        _case("a", 60),
        _case("b", None),  # 缺耗时 → 用 default_duration_s=30
        _case("c", 30),
    ]
    shards = split_by_time(cases, target_duration_s=60, default_duration_s=30)
    # a(60) 单独一 shard；b(30 默认)+c(30) 一 shard
    assert [s.case_keys for s in shards] == [("a",), ("b", "c")]


def test_split_by_time_single_large_case_overflows() -> None:
    """单 case 超过目标时长时仍单独成 shard，不丢失。"""
    cases = [_case("a", 100), _case("b", 10)]
    shards = split_by_time(cases, target_duration_s=50, default_duration_s=50)
    assert [s.case_keys for s in shards] == [("a",), ("b",)]


def test_split_by_time_empty_cases() -> None:
    assert split_by_time([], target_duration_s=60) == []


def test_split_by_time_rejects_non_positive_target() -> None:
    with pytest.raises(SplitPolicyError):
        split_by_time([_case("a", 1)], target_duration_s=0)


def test_split_by_time_rejects_non_positive_default() -> None:
    with pytest.raises(SplitPolicyError):
        split_by_time([_case("a", None)], target_duration_s=60, default_duration_s=0)
