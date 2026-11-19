"""test_core 插件配置：``config.ini`` 的 ``[test_core]`` 节（DESIGN §13）。

字段声明与校验由 :class:`common.config_base.FieldConfig` 驱动：类属性即默认值，
``ini_section`` 接入 appconfig 的默认值注入与 ``fiber.update`` 写回。超时字段
收敛为有限数（``task_timeout`` 允许 0 = 不限），计数字段收敛为非负 int。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import ClassVar

from common.config_base import FieldConfig, Issue


def _positive_seconds(name: str, raw: object) -> float:
    """把配置值收敛成**有限正数**秒；不满足则抛异常（类型错 TypeError）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{name} 需是有限正数，收到 {raw!r}")
    try:
        value = float(raw)
    except OverflowError as exc:
        raise ValueError(f"{name} 需是有限正数，收到 {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 需是有限正数，收到 {raw!r}")
    return value


def _non_negative_seconds(name: str, raw: object) -> float:
    """把配置值收敛成**有限非负数**秒（0 = 不限）；不满足则抛异常（类型错 TypeError）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{name} 需是有限非负数，收到 {raw!r}")
    try:
        value = float(raw)
    except OverflowError as exc:
        raise ValueError(f"{name} 需是有限非负数，收到 {raw!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} 需是有限非负数，收到 {raw!r}")
    return value


def _non_negative_int(name: str, raw: object) -> int:
    """把配置值收敛成**非负 int**（bool 拒绝）；不满足则抛异常（类型错 TypeError）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{name} 需是非负整数，收到 {raw!r}")
    if raw < 0:
        raise ValueError(f"{name} 需是非负整数，收到 {raw!r}")
    return raw


def _positive_int(name: str, raw: object) -> int:
    """把配置值收敛成**正 int**（bool 拒绝）；不满足则抛异常（类型错 TypeError）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{name} 需是正整数，收到 {raw!r}")
    if raw <= 0:
        raise ValueError(f"{name} 需是正整数，收到 {raw!r}")
    return raw


class TestCoreConfig(FieldConfig):
    """test_core 插件配置；类属性即默认值（DESIGN §13）。

    配置来源分三层（后者覆盖前者）：类属性默认值 → ``config.ini`` 的
    ``[test_core]`` 节（``internal/config`` 钩子注入）→ 显式传入 /
    ``fiber.update``。
    """

    #: 对应 ``config.ini`` 的 ``[test_core]`` 节（注入默认值 + 写回）
    ini_section: ClassVar[str | None] = "test_core"
    #: 字段类型表：字段名 -> (类型元组, 说明)
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "lease_timeout": ((int, float), "lease_timeout（Job 租约秒数：发出 → 结果/续期）"),
        "job_timeout": ((int, float), "job_timeout（Job 默认超时秒数，StageSpec.timeout_s=0 时生效）"),
        "task_timeout": ((int, float), "task_timeout（任务默认总超时秒数，0=不限）"),
        "max_attempts": ((int,), "max_attempts（单 Job 最大执行次数，含首次）"),
        "max_inflight_jobs": ((int,), "max_inflight_jobs（全局派发窗口）"),
        "max_inflight_per_node": ((int,), "max_inflight_per_node（单节点在途上限）"),
        "default_priority": ((int,), "default_priority（未指定时的优先级）"),
        "project_quota": ((int,), "project_quota（按 labels.project 的并发配额，0=不限）"),
        "case_results_limit": ((int,), "case_results_limit（单 Job 用例结果截断上限）"),
        "retention_days": ((int,), "retention_days（产物本地保留天数，GC 插件消费）"),
        "history_size": ((int,), "history_size（task_history 单任务保留条数）"),
        "material_max_bytes": ((int,), "material_max_bytes（材料上传字节上限）"),
        "materials_dir": ((str,), "materials_dir（材料存储目录，空 = data/plugins/test_core/materials）"),
    }

    #: Job 租约秒数（发出 → 结果/续期）
    lease_timeout: float = 300.0
    #: Job 默认超时秒数（StageSpec.timeout_s = 0 时生效）
    job_timeout: float = 3600.0
    #: 任务默认总超时秒数（0 = 不限）
    task_timeout: float = 0.0
    #: 单 Job 最大执行次数（含首次）
    max_attempts: int = 3
    #: 全局派发窗口
    max_inflight_jobs: int = 32
    #: 单节点在途上限
    max_inflight_per_node: int = 4
    #: 未指定时的优先级
    default_priority: int = 0
    #: 按 labels.project 的并发配额（0 = 不限）
    project_quota: int = 0
    #: 单 Job 用例结果截断上限
    case_results_limit: int = 2000
    #: 产物本地保留天数（GC 插件消费）
    retention_days: int = 30
    #: task_history 单任务保留条数
    history_size: int = 200
    #: 材料上传字节上限（executor_pytest DESIGN §12.1）
    material_max_bytes: int = 16777216
    #: 材料存储目录（空 = 运行时取 data/plugins/test_core/materials，解析基准同 db/log）
    materials_dir: str = ""

    def _validate(self) -> list[Issue]:
        """校验与归一化：超时收敛有限数、计数收敛非负 int、max_attempts ≥ 1、
        material_max_bytes 正 int、materials_dir 去首尾空白。

        问题以 ``Issue`` 列表返回（不抛），命中即归一化写回属性。
        """
        issues: list[Issue] = []

        def collect(name: str, coerce: Callable[[str, object], float | int]) -> None:
            try:
                setattr(self, name, coerce(name, getattr(self, name)))
            except (TypeError, ValueError) as exc:
                issues.append({"message": f"配置 [test_core].{exc}", "path": [name]})

        collect("lease_timeout", _positive_seconds)
        collect("job_timeout", _positive_seconds)
        collect("task_timeout", _non_negative_seconds)
        collect("material_max_bytes", _positive_int)
        for name in (
            "max_attempts",
            "max_inflight_jobs",
            "max_inflight_per_node",
            "default_priority",
            "project_quota",
            "case_results_limit",
            "retention_days",
            "history_size",
        ):
            collect(name, _non_negative_int)
        if isinstance(self.materials_dir, str):
            self.materials_dir = self.materials_dir.strip()
        attempts = self.max_attempts
        if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts < 1:
            issues.append(
                {
                    "message": f"配置 [test_core].max_attempts 需 ≥ 1（含首次），收到 {attempts}",
                    "path": ["max_attempts"],
                }
            )
        return issues

    def _validate_extra(self) -> list[Issue]:
        """基类校验钩子（``FieldConfig.validate`` 调用）；实现见 :meth:`_validate`。"""
        return self._validate()


__all__: list[str] = ["TestCoreConfig"]
