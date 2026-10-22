"""主从通信服务插件（``common/plugins/comm``）：全系统唯一的主从消息通道。

对外契约（``docs/comm通信方案-2026-10-22.md``）::

    from common.plugins.comm import CommService

    await ctx.plugin(CommService)          # 依赖 appconfig / db / webapi

    # 发送（入队即返回 message_id；policy 可选，见 DeliveryPolicy）
    ctx.comm.broadcast("task.notice", {...})        # 广播：任一目标成功即可
    ctx.comm.send_to("slave-1", "task.assign", {...})  # 定向
    ctx.comm.report("log.batch", {...})             # 上行至汇聚点（主节点）

    # 接收：comm.on 与 ctx.on("comm/{topic}") 等价（cordis 事件总线派发）
    ctx.comm.on("task.assign", handler)             # handler(payload, meta)

    ctx.comm.status(message_id)          # 单条投递状态
    await ctx.comm.flush()               # 排空在途消息（测试 / 关停）

``[comm] target`` 留空 = 本节点即汇聚点（本地自环）；填主节点地址 = 上行。
至少投递 1 次的消息经 db 插件持久化（成功即删、重启续传）；心跳等容忍
丢失的流量用 ``policy=FIRE_AND_FORGET, immediate=True``。
"""

from __future__ import annotations

from .config import CommConfig
from .interface import (
    AT_LEAST_ONCE,
    FIRE_AND_FORGET,
    CommInterface,
    CommMeta,
    CommTopicHandler,
    CommTransport,
    DeliveryPolicy,
    Envelope,
    MessageState,
    MessageStatus,
    TransportResult,
    bounded_policy,
    resolve_policy,
)
from .service import CommService

__all__: list[str] = [
    "AT_LEAST_ONCE",
    "FIRE_AND_FORGET",
    "CommConfig",
    "CommInterface",
    "CommMeta",
    "CommService",
    "CommTopicHandler",
    "CommTransport",
    "DeliveryPolicy",
    "Envelope",
    "MessageState",
    "MessageStatus",
    "TransportResult",
    "bounded_policy",
    "resolve_policy",
]
