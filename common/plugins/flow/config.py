"""flow 引擎配置：``config.ini`` 的 ``[flow]`` 节（设计 §5.15）。

字段声明与校验由 :class:`common.config_base.FieldConfig` 驱动；未启用的
Phase（远程 / 保留）字段先行声明，接入时不再改契约（只加不改）。
"""

from __future__ import annotations

import math
from typing import ClassVar

from common.config_base import FieldConfig, Issue


def _finite(name: str, raw: object, *, minimum: float = 0.0) -> float:
    """收敛为有限数且不小于 ``minimum``；不满足抛异常（类型错 TypeError）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{name} 需是不小于 {minimum} 的有限数，收到 {raw!r}")
    try:
        value = float(raw)
    except OverflowError as exc:
        raise ValueError(f"{name} 需是不小于 {minimum} 的有限数，收到 {raw!r}") from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} 需是不小于 {minimum} 的有限数，收到 {raw!r}")
    return value


class FlowConfig(FieldConfig):
    """flow 引擎配置；类属性即默认值（§5.15 的 [flow] 节）。"""

    #: 对应 ``config.ini`` 的 ``[flow]`` 节
    ini_section: ClassVar[str | None] = "flow"
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "max_concurrent_runs": ((int,), "max_concurrent_runs（并发运行上限，超出排队）"),
        "step_timeout": ((int, float), "step_timeout（节点缺省单次尝试超时秒数，0 = 不限）"),
        "cancel_grace": ((int, float), "cancel_grace（取消 / 超时后给步骤收尾的秒数）"),
        "drain_timeout": ((int, float), "drain_timeout（提供者卸载时等待在途步骤的秒数）"),
        "resolve_wait": ((int, float), "resolve_wait（必选节点缺提供者时等待出现的秒数）"),
        "retry_cap": ((int, float), "retry_cap（重试退避上限秒数）"),
        "resume_delay": ((int, float), "resume_delay（重启后恢复在途运行前的等待秒数）"),
        "place_wait": ((int, float), "place_wait（远程作业等待在线节点的秒数，Phase 4）"),
        "lease_interval": ((int, float), "lease_interval（远程续租间隔秒数，Phase 4）"),
        "lease_timeout": ((int, float), "lease_timeout（远程租约超时秒数，Phase 4）"),
        "max_output_bytes": ((int,), "max_output_bytes（单节点输出上限字节）"),
        "max_input_bytes": ((int,), "max_input_bytes（运行输入上限字节）"),
        "max_blackboard_bytes": ((int,), "max_blackboard_bytes（黑板总量上限字节）"),
        "max_remote_payload_bytes": ((int,), "max_remote_payload_bytes（远程载荷上限，Phase 4）"),
        "retain_days": ((int,), "retain_days（运行记录保留天数，Phase 3）"),
        "retain_max_runs": ((int,), "retain_max_runs（运行记录保留条数，Phase 3）"),
    }

    #: 并发运行上限；超出的 run 保持 pending、FIFO 排队
    max_concurrent_runs: int = 8
    #: 节点缺省单次尝试超时（秒）；0 = 不限
    step_timeout: float = 3600.0
    #: 取消 / 超时后给步骤收尾的时间（秒）
    cancel_grace: float = 10.0
    #: 提供者卸载时等待在途步骤的时限（秒）
    drain_timeout: float = 30.0
    #: 必选节点缺提供者时等待提供者出现的时长（秒）；0 = 立即失败
    resolve_wait: float = 0.0
    #: 重试退避上限（秒）
    retry_cap: float = 60.0
    #: 重启后恢复在途运行前的等待（秒）——容忍心跳注册表的空窗（§5.10 / §5.15）
    resume_delay: float = 15.0
    #: 远程作业等待具备能力的在线节点的时长（秒，Phase 4）
    place_wait: float = 60.0
    #: 远程步骤续租间隔（秒，Phase 4）
    lease_interval: float = 10.0
    #: 租约超时（秒，Phase 4）
    lease_timeout: float = 45.0
    #: 单节点输出上限（字节）
    max_output_bytes: int = 262144
    #: 运行输入上限（字节）
    max_input_bytes: int = 262144
    #: 黑板总量上限（字节）
    max_blackboard_bytes: int = 4194304
    #: 远程作业内联载荷上限（字节，Phase 4）
    max_remote_payload_bytes: int = 65536
    #: 运行记录保留天数（Phase 3）
    retain_days: int = 30
    #: 运行记录保留条数（Phase 3）
    retain_max_runs: int = 5000

    def _validate_extra(self) -> list[Issue]:
        """数值收敛：计数类为正整数，秒数 / 字节为有限非负数。"""
        issues: list[Issue] = []
        for name in ("max_concurrent_runs",):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                issues.append(
                    {
                        "message": f"配置 [flow].{name} 需是正整数，收到 {value!r}",
                        "path": [name],
                    }
                )
        for name in (
            "step_timeout",
            "cancel_grace",
            "drain_timeout",
            "resolve_wait",
            "retry_cap",
            "resume_delay",
            "place_wait",
            "lease_interval",
            "lease_timeout",
            "max_output_bytes",
            "max_input_bytes",
            "max_blackboard_bytes",
            "max_remote_payload_bytes",
            "retain_days",
            "retain_max_runs",
        ):
            try:
                setattr(self, name, _finite(name, getattr(self, name)))
            except (TypeError, ValueError) as exc:
                issues.append({"message": f"配置 [flow].{exc}", "path": [name]})
        return issues


__all__: list[str] = ["FlowConfig"]
