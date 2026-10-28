"""api-stats 插件配置：``config.ini`` 的 ``[api-stats]`` 节。

字段声明与校验由 :class:`common.config_base.FieldConfig` 驱动；
``ini_section`` 接入 appconfig 的默认值注入与 ``fiber.update`` 写回。
"""

from __future__ import annotations

import math
from typing import ClassVar

from common.config_base import FieldConfig, Issue


class ApiStatsConfig(FieldConfig):
    """api-stats 配置；类属性即默认值（设计 §4.6）。"""

    #: 对应 ``config.ini`` 的 ``[api-stats]`` 节
    ini_section: ClassVar[str | None] = "api-stats"
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "log_interval": ((int, float), "log_interval（热点统计输出周期秒数）"),
        "top_n": ((int,), "top_n（热点 Top-N 条数）"),
        "exclude_prefixes": (
            (str, tuple, list),
            "exclude_prefixes（排除的路径前缀，逗号分隔；机器通道不统计）",
        ),
    }

    #: 热点 Top-N 输出周期（秒）；聚合后周期输出，不逐请求写日志
    log_interval: float = 60.0
    #: 热点条数
    top_n: int = 10
    #: 排除的路径前缀（默认机器通道：心跳 / comm 长轮询 / 健康探针）
    exclude_prefixes: tuple[str, ...] = ("/api/heartbeat", "/api/comm", "/healthz")

    def _validate_extra(self) -> list[Issue]:
        """log_interval 为有限正数、top_n 为正整数；前缀接受逗号串 / 列表。"""
        issues: list[Issue] = []
        try:
            value = float(self.log_interval)
        except (TypeError, OverflowError) as exc:
            value = 0.0
            issues.append(
                {
                    "message": f"配置 [api-stats].log_interval 需是有限正数：{exc}",
                    "path": ["log_interval"],
                }
            )
        if not math.isfinite(value) or value <= 0:
            if not issues:
                issues.append(
                    {
                        "message": f"配置 [api-stats].log_interval 需是有限正数，收到 {self.log_interval!r}",
                        "path": ["log_interval"],
                    }
                )
        else:
            self.log_interval = value
        if not isinstance(self.top_n, int) or isinstance(self.top_n, bool) or self.top_n <= 0:
            issues.append(
                {
                    "message": f"配置 [api-stats].top_n 需是正整数，收到 {self.top_n!r}",
                    "path": ["top_n"],
                }
            )
        raw = self.exclude_prefixes
        if isinstance(raw, str):
            self.exclude_prefixes = tuple(p.strip() for p in raw.split(",") if p.strip())
        elif isinstance(raw, list):
            self.exclude_prefixes = tuple(str(p) for p in raw)
        return issues


__all__: list[str] = ["ApiStatsConfig"]
