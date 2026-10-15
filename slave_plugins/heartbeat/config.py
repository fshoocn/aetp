"""心跳插件配置：``config.ini`` 的 ``[heartbeat]`` 节。

主模块地址由**节点配置**提供（不走 ``plugin.json`` 的 ``config``）::

    [heartbeat]
    master_url = http://127.0.0.1:8080   # 主模块基地址（必填）
    path = /api/heartbeat                 # 心跳接口路径（缺省 /api/heartbeat）
    interval = 5.0                        # 心跳间隔秒数（缺省 5，启动兜底值）
    timeout = 3.0                         # 单次请求超时秒数（缺省 3，启动兜底值）
    max_backoff = 60.0                    # 失败重试间隔上限秒数（缺省 60，启动兜底值）

**运行参数由主模块设定**：``interval`` / ``timeout`` / ``max_backoff`` 的 ini
值只是「主模块下发前」的启动兜底 —— 心跳响应携带 ``config`` 时从模块立即
应用（见 :meth:`HeartbeatConfig.apply_server`），主模块改配置后最迟一个
心跳周期内同步到全部从模块，不需要逐台改 ini。

失败重试采用指数退避：第 n 次连续失败后等待
``min(interval * 2**(n-1), max_backoff)`` 秒。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import ClassVar

from common.config_base import FieldConfig, Issue

#: ``[heartbeat]`` 节允许的键
_KEYS: frozenset[str] = frozenset(
    {"master_url", "path", "interval", "timeout", "max_backoff"}
)

#: 主模块可下发覆盖的运行参数；其余键（master_url / path）只由本地
#: config.ini 管理 —— 防止主模块把从模块指向别的地址。
SERVER_KEYS: frozenset[str] = frozenset({"interval", "timeout", "max_backoff"})


def _positive_seconds(name: str, raw: object) -> float:
    """把配置值收敛成**有限正数**秒；不满足则抛异常。

    异常形态对齐 stdlib 惯例（``float([])`` 抛 ``TypeError``、
    ``float("x")`` 抛 ``ValueError``）：类型不对抛 ``TypeError``，
    数值非法抛 ``ValueError``。三个坑一并挡掉：``bool``（``int`` 的
    子类）、超大整数（``float(10**400)`` 会抛 ``OverflowError``）、
    ``inf`` / ``nan``（``nan <= 0`` 恒为 False，会溜过「正数」检查，
    进而在 ``asyncio.sleep`` / ``settimeout`` 里炸出怪异异常）。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{name} 需是有限正数，收到 {raw!r}")
    try:
        value = float(raw)
    except OverflowError as exc:
        raise ValueError(f"{name} 需是有限正数，收到 {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 需是有限正数，收到 {raw!r}")
    return value


class HeartbeatConfig(FieldConfig):
    """心跳插件配置；类属性即默认值，实例由 :meth:`from_ini` 从 ini 构建。

    ``ini_section = "heartbeat"`` 把 ``config.ini`` 的 ``[heartbeat]`` 节接到
    appconfig 的配置注入 / 写回钩子上：加载时注入默认值，``fiber.update``
    提交时把新值写回该节。类型错误的说明内嵌字段名（如 ``interval``），
    便于用户对照 ini 键名定位。
    """

    #: 对应 ``config.ini`` 的 ``[heartbeat]`` 节
    ini_section: ClassVar[str | None] = "heartbeat"
    #: 字段类型表：字段名 -> (类型元组, 说明)；驱动默认值、类型校验与 ini 序列化
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "master_url": ((str,), "master_url（主模块基地址）"),
        "path": ((str,), "path（心跳接口路径）"),
        "interval": ((int, float), "interval（心跳间隔秒数）"),
        "timeout": ((int, float), "timeout（单次请求超时秒数）"),
        "max_backoff": ((int, float), "max_backoff（失败重试间隔上限秒数）"),
    }

    #: 主模块基地址（必填），如 ``http://127.0.0.1:8080``
    master_url: str = ""
    #: 心跳接口路径
    path: str = "/api/heartbeat"
    #: 心跳间隔（秒）；主模块可下发覆盖
    interval: float = 5.0
    #: 单次请求超时（秒）；主模块可下发覆盖
    timeout: float = 3.0
    #: 失败重试间隔上限（秒）；主模块可下发覆盖
    max_backoff: float = 60.0

    def endpoint(self) -> str:
        """完整心跳接口地址（``master_url`` + ``path``）。"""
        return f"{self.master_url.rstrip('/')}/{self.path.lstrip('/')}"

    def _validate_extra(self) -> list[Issue]:
        """master_url 必填与协议、path 前缀、三个运行参数收敛为有限正数。"""
        issues: list[Issue] = []
        if not self.master_url:
            issues.append(
                {
                    "message": "config.ini 的 [heartbeat] 节缺少 master_url"
                    "（主模块地址，如 http://127.0.0.1:8080）",
                    "path": ["master_url"],
                }
            )
        elif not self.master_url.startswith(("http://", "https://")):
            issues.append(
                {
                    "message": f"配置 [heartbeat].master_url 需以 http:// 或 https:// 开头："
                    f"{self.master_url!r}",
                    "path": ["master_url"],
                }
            )
        if not self.path.startswith("/"):
            issues.append(
                {
                    "message": f"配置 [heartbeat].path 需以 / 开头：{self.path!r}",
                    "path": ["path"],
                }
            )
        for name in ("interval", "timeout", "max_backoff"):
            try:
                setattr(self, name, _positive_seconds(name, getattr(self, name)))
            except (TypeError, ValueError) as exc:
                issues.append({"message": f"配置 [heartbeat].{exc}", "path": [name]})
        return issues

    # -- 主模块下发（配置同步） ---------------------------------------------
    def apply_server(
        self, values: Mapping[str, object]
    ) -> tuple[dict[str, tuple[float, float]], dict[str, str]]:
        """应用主模块下发的运行参数（心跳响应的 ``config`` 字段）。

        只接受 :data:`SERVER_KEYS`；值必须是有限正数（``inf`` / ``nan`` /
        超大整数一并拒绝）。**不抛异常**——非法值与仅限本地的配置项一律
        跳过（保留原值），主模块下发错误配置不应打挂从模块；完全未知的
        键静默忽略（向前兼容主模块后续新增项）。

        返回:
            ``(applied, skipped)``：applied 为 ``{键: (旧值, 新值)}``（仅含
            实际变更项），skipped 为 ``{键: 跳过原因}``。
        """
        applied: dict[str, tuple[float, float]] = {}
        skipped: dict[str, str] = {}
        for key, raw in values.items():
            name = str(key)
            if name not in _KEYS:
                continue  # 未知键静默忽略，向前兼容
            if name not in SERVER_KEYS:
                skipped[name] = "该配置项仅由本地 config.ini 管理"
                continue
            try:
                value = _positive_seconds(name, raw)
            except (TypeError, ValueError) as exc:
                skipped[name] = str(exc)
                continue
            old = float(getattr(self, name))
            if value != old:
                setattr(self, name, value)
                applied[name] = (old, value)
        return applied, skipped
