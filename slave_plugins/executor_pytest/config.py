"""pytest 执行端配置：``config.ini`` 的 ``[executor_pytest]`` 节（DESIGN §12.3）。

字段声明与校验由 :class:`common.config_base.FieldConfig` 驱动（风格照
``common/plugins/comm/config.py``，校验照 ``master_plugins/test_core/config.py``）：
类属性即默认值，``ini_section`` 接入 appconfig 的默认值注入与
``fiber.update`` 写回。超时 / 间隔收敛为有限正数，天数与容量收敛为有限非负数，
计数类收敛为非负 ``int``（bool 一律拒绝——``bool`` 是 ``int`` 子类）。
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


def _non_negative_float(name: str, raw: object) -> float:
    """把配置值收敛成**有限非负数**；不满足则抛异常（类型错 TypeError）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{name} 需是有限非负数，收到 {raw!r}")
    try:
        value = float(raw)
    except OverflowError as exc:
        raise ValueError(f"{name} 需是有限非负数，收到 {raw!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} 需是有限非负数，收到 {raw!r}")
    return value


def _positive_int(name: str, raw: object) -> int:
    """把配置值收敛成**正 int**（bool 拒绝）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{name} 需是正整数，收到 {raw!r}")
    if raw < 1:
        raise ValueError(f"{name} 需是正整数，收到 {raw!r}")
    return raw


def _non_negative_int(name: str, raw: object) -> int:
    """把配置值收敛成**非负 int**（bool 拒绝）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeError(f"{name} 需是非负整数，收到 {raw!r}")
    if raw < 0:
        raise ValueError(f"{name} 需是非负整数，收到 {raw!r}")
    return raw


class PytestExecutorConfig(FieldConfig):
    """pytest 执行端配置；类属性即默认值（DESIGN §12.3）。

    配置来源分三层（后者覆盖前者）：类属性默认值 → ``config.ini`` 的
    ``[executor_pytest]`` 节（``internal/config`` 钩子注入）→ 显式传入 /
    ``fiber.update``。
    """

    #: 对应 ``config.ini`` 的 ``[executor_pytest]`` 节（注入默认值 + 写回）
    ini_section: ClassVar[str | None] = "executor_pytest"
    #: 字段类型表：字段名 -> (类型元组, 说明)
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "python": ((str,), "python（解释器路径，空 = sys.executable）"),
        "pytest_args": ((str,), "pytest_args（默认附加参数，空格分隔）"),
        "timeout_s": ((int, float), "timeout_s（单 Job 超时秒数）"),
        "max_concurrent": ((int,), "max_concurrent（节点内并发执行数）"),
        "shard_files": ((int,), "shard_files（plan 分片每片文件数上限）"),
        "workdir": ((str,), "workdir（工作目录基址，空 = 节点目录）"),
        "material_wait_s": ((int, float), "material_wait_s（材料就绪宽限秒数）"),
        "material_retain_days": ((int, float), "material_retain_days（材料缓存保留天数）"),
        "material_cache_max_bytes": ((int,), "material_cache_max_bytes（材料缓存字节上限）"),
        "log_batch_lines": ((int,), "log_batch_lines（日志批量行数阈值）"),
        "log_batch_interval": ((int, float), "log_batch_interval（日志批量秒数阈值）"),
        "log_rate_limit": ((int,), "log_rate_limit（每秒日志包数上限）"),
        "line_max_bytes": ((int,), "line_max_bytes（单行截断字节数）"),
    }

    #: 解释器路径；空 = ``sys.executable``
    python: str = ""
    #: 默认附加参数（空格分隔，``shlex`` 口径拆分）
    pytest_args: str = "-p no:cacheprovider"
    #: 单 Job 超时（秒）
    timeout_s: float = 3600.0
    #: 节点内并发执行数
    max_concurrent: int = 1
    #: plan 分片每片文件数上限
    shard_files: int = 4
    #: 工作目录基址；空 = 节点目录（组装目录 = ``{workdir}/workdir/{job}/a{attempt}``）
    workdir: str = ""
    #: 材料就绪宽限等待（秒，§5.2）
    material_wait_s: float = 60.0
    #: 材料缓存保留天数（§3.2 双限之一）
    material_retain_days: float = 30.0
    #: 材料缓存字节上限（§3.2 双限之一，默认 2 GiB）
    material_cache_max_bytes: int = 2147483648
    #: 日志批量行数阈值（§10.4）
    log_batch_lines: int = 50
    #: 日志批量秒数阈值（§10.4）
    log_batch_interval: float = 2.0
    #: 每秒日志包数上限（超出合并，§10.4）
    log_rate_limit: int = 20
    #: 单行截断字节数（§10.4）
    line_max_bytes: int = 8192

    def _validate_extra(self) -> list[Issue]:
        """校验与归一化：超时收敛有限正数、容量/天数收敛非负、计数收敛正 int。"""
        issues: list[Issue] = []

        def collect(name: str, coerce: Callable[[str, object], float | int]) -> None:
            try:
                setattr(self, name, coerce(name, getattr(self, name)))
            except (TypeError, ValueError) as exc:
                issues.append({"message": f"配置 [executor_pytest].{exc}", "path": [name]})

        for name in ("timeout_s", "material_wait_s", "log_batch_interval"):
            collect(name, _positive_seconds)
        collect("material_retain_days", _non_negative_float)
        collect("material_cache_max_bytes", _non_negative_int)
        for name in (
            "max_concurrent",
            "shard_files",
            "log_batch_lines",
            "log_rate_limit",
            "line_max_bytes",
        ):
            collect(name, _positive_int)
        return issues


__all__: list[str] = ["PytestExecutorConfig"]
