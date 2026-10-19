"""主从通信服务插件（``common/plugins/comm``）：业务插件零感知的数据通道。

对外契约（详见 ``docs/日志方案-2026-10-19.md`` §5.1）::

    from common.plugins.comm import CommService

    await ctx.plugin(CommService)          # 依赖 webapi，读 ``[comm]`` 节

    # 发送侧（任意插件）：入队即返回，fire-and-forget
    ctx.comm.send("log.batch", {"logs": [...]})

    # 接收侧（汇聚点）：按 topic 注册处理器，返回注销函数
    ctx.comm.on("log.batch", handler)      # handler(payload, meta)

    await ctx.comm.flush()                 # 排空队列（测试 / 关停）

``[comm] target`` 留空 = 本节点即汇聚点（本地自环，不走网络）；填主节点
地址 = 批量 HTTP 上报。批量 / 退避重试 / 背压（丢最旧 + 计数）由本层
统一承担，业务插件不感知传输方式。
"""

from __future__ import annotations

from .config import CommConfig
from .service import CommMeta, CommService, CommTopicHandler

__all__: list[str] = ["CommConfig", "CommMeta", "CommService", "CommTopicHandler"]
