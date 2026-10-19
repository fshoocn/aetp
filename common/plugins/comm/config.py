"""主从通信服务配置模型（``[comm]`` 节）。"""

from __future__ import annotations

from typing import ClassVar

from common.config_base import FieldConfig


class CommConfig(FieldConfig):
    """主从通信配置。

    类属性即默认值；``ctx.plugin(CommService, {...})`` 传字典可覆盖；
    ``config.ini`` 的 ``[comm]`` 节经 ``internal/config`` 钩子注入。
    """

    ini_section: ClassVar[str | None] = "comm"
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "target": ((str,), "主节点基地址（留空 = 本节点即汇聚点，本地自环）"),
        "batch_interval": ((int, float), "批量触发间隔（秒）"),
        "batch_size": ((int,), "单批最大条数"),
        "queue_max": ((int,), "发送队列上限（超出丢最旧并计数）"),
        "retry_base": ((int, float), "重试退避基数（秒）"),
        "retry_cap": ((int, float), "重试退避上限（秒）"),
    }

    target: str = ""
    batch_interval: float = 2.0
    batch_size: int = 200
    queue_max: int = 2000
    retry_base: float = 1.0
    retry_cap: float = 60.0

    def __repr__(self) -> str:
        return f"CommConfig(target={self.target!r}, batch_interval={self.batch_interval})"


__all__: list[str] = ["CommConfig"]
