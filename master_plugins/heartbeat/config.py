"""主模块心跳插件配置：``config.ini`` 的 ``[heartbeat]`` 节（主模块侧）。

参数分两组：

* **下发参数**（``interval`` / ``timeout`` / ``max_backoff``）——心跳应答的
  ``config`` 字段带给从模块，运行参数由**主模块设定**（从模块 ini 值只是
  启动兜底，见 :mod:`slave_plugins.heartbeat.config`）；
* **判定阈值**（``offline_timeout`` / ``lost_timeout`` / ``sweep_interval`` /
  ``history_size``）——主模块侧的在线判定与状态历史。

字段声明与校验由 :class:`common.config_base.FieldConfig` 驱动；
``ini_section`` 接入 appconfig 的默认值注入与 ``fiber.update`` 写回。
"""

from __future__ import annotations

import math
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


class HeartbeatServerConfig(FieldConfig):
    """主模块心跳插件配置；类属性即默认值。

    配置来源分三层（后者覆盖前者）：类属性默认值 → ``config.ini`` 的
    ``[heartbeat]`` 节（``internal/config`` 钩子注入）→ 显式传入 /
    ``fiber.update``。默认阈值按 ``interval = 5`` 估算：
    ``offline_timeout = 3 × interval``、``lost_timeout = 10 × interval``。
    """

    #: 对应 ``config.ini`` 的 ``[heartbeat]`` 节（注入默认值 + 写回）
    ini_section: ClassVar[str | None] = "heartbeat"
    #: 字段类型表：字段名 -> (类型元组, 说明)；说明内嵌字段名便于对照 ini 键
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "interval": ((int, float), "interval（下发的心跳间隔秒数）"),
        "timeout": ((int, float), "timeout（下发的单次请求超时秒数）"),
        "max_backoff": ((int, float), "max_backoff（下发的失败重试上限秒数）"),
        "offline_timeout": ((int, float), "offline_timeout（判离线秒数）"),
        "lost_timeout": ((int, float), "lost_timeout（判失联秒数）"),
        "sweep_interval": ((int, float), "sweep_interval（扫描周期秒数）"),
        "history_size": ((int,), "history_size（状态历史保留条数）"),
    }

    # -- 下发给从模块的运行参数 ---------------------------------------------
    #: 下发的心跳间隔（秒）
    interval: float = 5.0
    #: 下发的单次请求超时（秒）
    timeout: float = 3.0
    #: 下发的失败重试间隔上限（秒）
    max_backoff: float = 60.0

    # -- 主模块侧判定阈值 -----------------------------------------------------
    #: 心跳超过该秒数判为 offline（缺省 3 × interval）
    offline_timeout: float = 15.0
    #: 超过该秒数判为 lost（缺省 10 × interval）
    lost_timeout: float = 50.0
    #: 状态扫描周期（秒）
    sweep_interval: float = 5.0
    #: 状态转换历史保留条数
    history_size: int = 200

    def _validate_extra(self) -> list[Issue]:
        """六个秒数收敛为有限正数，history_size 为正整数，阈值前后有序。"""
        issues: list[Issue] = []
        seconds = (
            "interval",
            "timeout",
            "max_backoff",
            "offline_timeout",
            "lost_timeout",
            "sweep_interval",
        )
        for name in seconds:
            try:
                setattr(self, name, _positive_seconds(name, getattr(self, name)))
            except (TypeError, ValueError) as exc:
                issues.append({"message": f"配置 [heartbeat].{exc}", "path": [name]})
        if self.history_size <= 0:
            issues.append(
                {
                    "message": f"配置 [heartbeat].history_size 需是正整数，收到 {self.history_size}",
                    "path": ["history_size"],
                }
            )
        if not issues and self.lost_timeout <= self.offline_timeout:
            issues.append(
                {
                    "message": "配置 [heartbeat].lost_timeout 需大于 offline_timeout，"
                    f"收到 {self.lost_timeout} ≤ {self.offline_timeout}",
                    "path": ["lost_timeout"],
                }
            )
        return issues


__all__: list[str] = ["HeartbeatServerConfig"]
