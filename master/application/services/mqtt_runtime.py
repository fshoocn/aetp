"""Master MQTT 运行时编排（P6.4，§9.6 阶段 C/E）。

``MasterMqttRuntime`` 把 Master 侧的 MQTT Transport、入站事件路由与事务性
outbox worker 串成一个生命周期：

1. 订阅全部 Agent 事件主题（``aetp/v1/agents/+/events/#``）；
2. 注册 ``MasterMessageRouter`` 为入站处理器；
3. 启动 OutboxWorker（事务性 outbox 可靠发送 run.assign / register-ack）；
4. 连接 Transport（断连自动重连，重连后恢复订阅）。

Master 的入站消费与出站发送解耦：入站走路由投影，出站走 outbox worker。
"""

from __future__ import annotations

import logging

from common.transport import Transport

logger = logging.getLogger(__name__)

# Agent -> Master 事件通配主题：aetp/v1/agents/{node_id}/events/{segment}
EVENTS_TOPIC_FILTER = "aetp/v1/agents/+/events/#"
V2_EVENTS_TOPIC_FILTER = "aetp/v2/agents/+/events/#"


class MasterMqttRuntime:
    """Master MQTT 生命周期协调者。"""

    def __init__(
        self,
        transport: Transport,
        router,
        outbox_worker,
        *,
        events_topic_filter: str = EVENTS_TOPIC_FILTER,
        v2_events_topic_filter: str = V2_EVENTS_TOPIC_FILTER,
        v2_only: bool = False,
    ) -> None:
        self._transport = transport
        self._router = router
        self._outbox_worker = outbox_worker
        self._events_topic_filter = events_topic_filter
        self._v2_events_topic_filter = v2_events_topic_filter
        self._v2_only = v2_only
        self._started = False

    async def start(self) -> None:
        """启动：注册处理器 → 订阅 → outbox worker → 连接（幂等）。"""
        if self._started:
            return
        self._started = True
        self._transport.on_message(self._router.handle)
        topics = [self._v2_events_topic_filter] if self._v2_only else [
            self._events_topic_filter,
            self._v2_events_topic_filter,
        ]
        await self._transport.subscribe(topics)
        await self._outbox_worker.start()
        await self._transport.connect()
        logger.info("Master MQTT runtime 已启动（订阅 %s）", topics)

    async def stop(self) -> None:
        """停止：outbox worker → 断开连接（幂等）。"""
        if not self._started:
            return
        self._started = False
        # 两个后台任务都实现取消语义；先停止 outbox，避免关闭期间继续
        # claim/publish，再断开 MQTT。CancelledError 由上层 wait_for 处理。
        await self._outbox_worker.stop()
        await self._transport.disconnect()
        logger.info("Master MQTT runtime 已停止")
