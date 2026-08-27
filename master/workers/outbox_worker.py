"""Outbox worker（P4.3，§9.6 阶段 C / §15.3 P4.3）。

事务性 outbox 的可靠发送端：
- 周期性 `claim_due` 取到期消息（事务性 claim → sending，防止并发重复发送）
- 经 Transport 端口发布（payload dict → JSON bytes；topic/qos 取自消息）
- 发送成功 → 标记 succeeded；失败 → 指数退避推进 attempts / next_attempt_at，
  超过 max_attempts → exhausted（断连期间消息停留在 outbox，恢复后继续发送）

worker 只依赖 UnitOfWork + Transport 端口，不接触具体 MQTT 客户端
（§4.2 依赖方向：workers → ports）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta

from common.backoff import ExponentialBackoff
from common.transport import Transport
from master.domain.enums import OutboxStatus
from master.domain.models import OutboxMessage
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger("master.workers.outbox")


class OutboxWorker:
    """Outbox 可靠发送 worker（后台轮询 + 单次 run_once 均可）。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        transport: Transport,
        *,
        poll_interval_s: float = 1.0,
        batch_size: int = 100,
        max_attempts: int = 5,
        retry_backoff: ExponentialBackoff | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._transport = transport
        self._poll_interval_s = poll_interval_s
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._backoff = retry_backoff or ExponentialBackoff()
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # -- 生命周期 -----------------------------------------------------------

    async def start(self) -> None:
        """启动后台轮询循环（幂等）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Outbox worker 启动（poll=%.1fs, batch=%d, max_attempts=%d）",
            self._poll_interval_s,
            self._batch_size,
            self._max_attempts,
        )

    async def stop(self) -> None:
        """停止轮询循环（幂等）。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Outbox worker 停止")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Outbox worker 轮询异常")
            await asyncio.sleep(self._poll_interval_s)

    # -- 一次轮询 -----------------------------------------------------------

    async def run_once(self) -> int:
        """取到期消息并逐条发送；返回本次处理的条数。

        每条消息独立处理：单条发送失败只影响该条（fail-open），
        不阻塞同批其余消息。
        """
        messages = self._claim()
        for message in messages:
            await self._send_one(message)
        return len(messages)

    def _claim(self) -> list[OutboxMessage]:
        """事务性 claim 到期消息（status → sending，attempts+1）。"""
        with self._uow_factory() as uow:
            return uow.outbox_messages.claim_due(limit=self._batch_size)

    async def _send_one(self, message: OutboxMessage) -> None:
        try:
            payload = json.dumps(message.payload).encode("utf-8")
            await self._transport.publish(message.topic, payload, qos=message.qos)
        except Exception as exc:  # noqa: BLE001 - 未连接/发送失败统一走重试/耗尽
            self._mark_failed(message, exc)
            return
        self._mark_succeeded(message)

    # -- 结果落库（各自独立事务，避免互相回滚） -------------------------------

    def _mark_succeeded(self, message: OutboxMessage) -> None:
        message.status = OutboxStatus.SUCCEEDED
        message.sent_at = utcnow()
        with self._uow_factory() as uow:
            uow.outbox_messages.update(message)
        logger.info("Outbox 发送成功: outbox_id=%s topic=%s qos=%d", message.outbox_id, message.topic, message.qos)

    def _mark_failed(self, message: OutboxMessage, exc: Exception) -> None:
        now = utcnow()
        if message.attempts >= self._max_attempts:
            message.status = OutboxStatus.EXHAUSTED
            message.next_attempt_at = None
            logger.error("Outbox 重试耗尽: outbox_id=%s topic=%s（%s）", message.outbox_id, message.topic, exc)
        else:
            message.status = OutboxStatus.RETRYING
            # claim_due 已将 attempts 加一；按当前消息的历史尝试次数计算退避，
            # 避免不同消息共享一个可变计数器导致退避被重置。
            delay = self._backoff.delay_for(message.attempts - 1)
            message.next_attempt_at = now + timedelta(seconds=delay)
            logger.warning(
                "Outbox 发送失败，%.1fs 后重试: outbox_id=%s topic=%s（%s）",
                delay,
                message.outbox_id,
                message.topic,
                exc,
            )
        with self._uow_factory() as uow:
            uow.outbox_messages.update(message)
