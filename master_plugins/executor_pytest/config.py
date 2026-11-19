"""executor_pytest 管理端插件配置：``config.ini`` 的 ``[executor_pytest_master]`` 节。

字段声明与校验由 :class:`common.config_base.FieldConfig` 驱动：类属性即默认值，
``ini_section`` 接入 appconfig 的默认值注入与 ``fiber.update`` 写回。数值字段
有限正数校验（inspect_timeout_s / material_retain_days），int 字段非负整数
校验（push_chunk_bytes / push_concurrency，运行侧再钳到 ≥1 防退化）。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import ClassVar

from common.config_base import FieldConfig, Issue


def _positive_number(name: str, raw: object) -> float:
    """把配置值收敛成**有限正数**（秒 / 天）；不满足则抛异常（类型错 TypeError）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{name} 需是有限正数，收到 {raw!r}")
    try:
        value = float(raw)
    except OverflowError as exc:
        raise ValueError(f"{name} 需是有限正数，收到 {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 需是有限正数，收到 {raw!r}")
    return value


def _non_negative_int(name: str, raw: object) -> int:
    """把配置值收敛成**非负 int**（bool 拒绝）；不满足则抛异常（类型错 TypeError）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{name} 需是非负整数，收到 {raw!r}")
    if raw < 0:
        raise ValueError(f"{name} 需是非负整数，收到 {raw!r}")
    return raw


class ExecPytestMasterConfig(FieldConfig):
    """executor_pytest 管理端配置（executor_pytest DESIGN §12.2）。

    配置来源分三层（后者覆盖前者）：类属性默认值 → ``config.ini`` 的
    ``[executor_pytest_master]`` 节（``internal/config`` 钩子注入）→ 显式传入 /
    ``fiber.update``。
    """

    #: 对应 ``config.ini`` 的 ``[executor_pytest_master]`` 节（注入默认值 + 写回）
    ini_section: ClassVar[str | None] = "executor_pytest_master"
    #: 字段类型表：字段名 -> (类型元组, 说明)
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "push_chunk_bytes": ((int,), "push_chunk_bytes（材料分块原文大小字节数）"),
        "push_concurrency": ((int,), "push_concurrency（并行预推节点数）"),
        "inspect_timeout_s": ((int, float), "inspect_timeout_s（分析/解析应答超时秒数）"),
        "material_retain_days": ((int, float), "material_retain_days（主端无引用材料保留天数）"),
    }

    #: 材料分块原文大小（字节；b64 后约 1.33 倍，DESIGN §5.1 默认 64KiB）
    push_chunk_bytes: int = 65536
    #: 并行预推节点数（预推限流）
    push_concurrency: int = 2
    #: 分析 / 解析应答超时（秒，§6.1 超时扫尾）
    inspect_timeout_s: float = 300.0
    #: 主端无引用材料保留天数（GC，§3.2）
    material_retain_days: float = 90.0

    def _validate(self) -> list[Issue]:
        """校验与归一化：int 字段非负整数、数值字段有限正数。

        问题以 ``Issue`` 列表返回（不抛），命中即归一化写回属性。
        """
        issues: list[Issue] = []

        def collect(name: str, coerce: Callable[[str, object], float | int]) -> None:
            try:
                setattr(self, name, coerce(name, getattr(self, name)))
            except (TypeError, ValueError) as exc:
                issues.append({"message": f"配置 [executor_pytest_master].{exc}", "path": [name]})

        collect("push_chunk_bytes", _non_negative_int)
        collect("push_concurrency", _non_negative_int)
        collect("inspect_timeout_s", _positive_number)
        collect("material_retain_days", _positive_number)
        return issues

    def _validate_extra(self) -> list[Issue]:
        """基类校验钩子（``FieldConfig.validate`` 调用）；实现见 :meth:`_validate`。"""
        return self._validate()


__all__: list[str] = ["ExecPytestMasterConfig"]
