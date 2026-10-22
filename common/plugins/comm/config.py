"""主从通信服务配置模型（``[comm]`` 节）。

协议不进配置（决策 A2）：HTTP 是默认实现、单选；将来换协议由新实现
替换默认装配，所需配置由新实现自行扩展。
"""

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
        "target": ((str,), "汇聚点（主节点）基地址（留空 = 本节点即汇聚点，本地自环）"),
        "batch_interval": ((int, float), "批量触发间隔（秒）"),
        "batch_size": ((int,), "单批最大条数"),
        "queue_max": ((int,), "在途消息上限（超出丢最旧并计数）"),
        "retry_base": ((int, float), "重试退避基数（秒）"),
        "retry_cap": ((int, float), "重试退避上限（秒）"),
        "default_min_success": ((int,), "默认策略：至少成功次数（任意一次成功即可）"),
        "default_min_retries": ((int,), "默认策略：至少重试次数"),
        "default_max_attempts": ((int,), "默认策略：至多发送次数（0 = 不限）"),
        "broadcast_ttl": ((int, float), "广播/定向默认 ttl（秒，0 = 永不过期）"),
        "poll_hang": ((int, float), "下行长轮询挂起时长（秒）"),
        "ack_timeout": ((int, float), "下行投递后等待 ack 的时限（秒）"),
        "outbox_max": ((int,), "每节点下行 outbox 上限（满丢最旧并计数）"),
    }

    target: str = ""
    batch_interval: float = 2.0
    batch_size: int = 200
    queue_max: int = 2000
    retry_base: float = 1.0
    retry_cap: float = 60.0
    default_min_success: int = 1
    default_min_retries: int = 0
    default_max_attempts: int = 0
    broadcast_ttl: float = 300.0
    poll_hang: float = 30.0
    ack_timeout: float = 60.0
    outbox_max: int = 2000

    def __repr__(self) -> str:
        return f"CommConfig(target={self.target!r}, batch_interval={self.batch_interval})"


__all__: list[str] = ["CommConfig"]
