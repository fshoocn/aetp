"""Agent MQTT transport（aiomqtt 实现，P5.3）。

复用 common.transport 端口与 common.backoff，连接/订阅/发布/指数退避
重连语义与 Master 的 MqttTransport 一致；Agent 订阅自己的专属 commands
主题（§9.7 启动顺序：创建 session_id 与固定 LWT 后连接 → 订阅 → 注册）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import uuid
from typing import Any, cast

import aiomqtt

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import PresencePayload

from agent.config import AgentSettings
from common.backoff import ExponentialBackoff
from common.transport import (
    ConnectionHandler,
    MessageHandler,
    MqttMessage,
    Transport,
    TransportError,
)

logger = logging.getLogger(__name__)


class AgentMqttTransport(Transport):
    """基于 aiomqtt 的 Agent MQTT 传输适配器。

    - 每次进程启动生成新的 ``session_id``（§9.7 规则 1）；
    - 连接携带固定 LWT（仅 node_id/reason/sent_at，§8.6）；
    - 断连指数退避重连，重连后恢复订阅。
    """

    def __init__(
        self,
        settings: AgentSettings,
        *,
        backoff: ExponentialBackoff | None = None,
    ) -> None:
        self._settings = settings
        self._backoff = backoff or ExponentialBackoff()
        self._subscribed: list[str] = []
        self._handler: MessageHandler | None = None
        self._connection_handler: ConnectionHandler | None = None
        self._client: aiomqtt.Client | None = None
        self._connected = False
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._session_id = uuid.uuid4().hex

    # -- Transport 端口契约 -------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    def on_connection_change(self, handler: ConnectionHandler) -> None:
        """注册连接状态变化处理器。"""
        self._connection_handler = handler

    @property
    def session_id(self) -> str:
        """当前 MQTT 连接会话 ID。"""
        return self._session_id

    async def connect(self) -> None:
        """启动后台连接/重连循环（幂等）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Agent MQTT transport 启动（重连循环）")

    async def disconnect(self) -> None:
        """停止循环并释放连接（幂等）。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._client = None
        self._connected = False

    async def subscribe(self, topics: list[str]) -> None:
        """更新订阅集合；已连接时立即订阅，重连后自动恢复。"""
        self._subscribed = list(topics)
        if self._connected and self._client is not None:
            for topic in topics:
                await self._client.subscribe(topic, qos=1)
            logger.info("Agent MQTT 已订阅 %d 个主题", len(topics))

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        if self._client is None or not self._connected:
            raise TransportError(f"MQTT 未连接，无法发布: {topic}")
        await self._client.publish(topic, payload, qos=qos)

    # -- 内部实现 -----------------------------------------------------------

    def _lwt_payload(self) -> bytes:
        """固定 LWT：使用完整 Envelope，携带当前 session（§8.6）。"""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        payload = PresencePayload(
            node_id=self._settings.node_id,
            reason="unexpected_disconnect",
            sent_at=now,
        )
        envelope = Envelope(
            message_id=uuid.uuid4().hex,
            message_type=MessageType.PRESENCE.value,
            sent_at=now,
            sender=Sender(
                kind=SenderKind.AGENT,
                id=self._settings.node_id,
                session_id=self._session_id,
            ),
            trace_id=self._settings.node_id,
            payload=payload.model_dump(mode="json"),
        )
        return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")

    def _client_kwargs(self) -> dict[str, Any]:
        """构造 aiomqtt.Client 参数（含 TLS 与固定 LWT）。"""
        s = self._settings
        kwargs: dict[str, Any] = {
            "hostname": s.mqtt_host or "127.0.0.1",
            "port": s.mqtt_port,
            # aiomqtt.Client 使用 identifier 参数；client_id 会导致运行时
            # TypeError，连接循环随后无限重连。
            "identifier": s.mqtt_client_id,
            "keepalive": 30,
            "will": aiomqtt.Will(
                topic=f"aetp/v1/agents/{s.node_id}/events/presence",
                payload=self._lwt_payload(),
                qos=1,
            ),
        }
        if s.mqtt_username:
            kwargs["username"] = s.mqtt_username
            kwargs["password"] = s.mqtt_password
        tls_context = self._build_tls_context()
        if tls_context is not None:
            kwargs["tls_context"] = tls_context
        return kwargs

    def _build_tls_context(self) -> ssl.SSLContext | None:
        """按配置构建 TLS 上下文（CA 证书路径）。"""
        if not self._settings.mqtt_use_tls:
            return None
        ctx = ssl.create_default_context()
        if self._settings.mqtt_ca_cert_path:
            ctx.load_verify_locations(
                cafile=str(self._settings.mqtt_ca_cert_path)
            )
        return ctx

    async def _run_loop(self) -> None:
        """连接 → 订阅 → 消费消息；断连指数退避重连。"""
        while self._running:
            try:
                # 每次底层连接都使用新 session，旧连接的迟到消息可被 Master 拒绝。
                self._session_id = uuid.uuid4().hex
                async with aiomqtt.Client(**self._client_kwargs()) as client:
                    self._client = client
                    self._connected = True
                    self._backoff.reset()
                    logger.info(
                        "Agent MQTT 已连接: %s:%s client=%s",
                        self._settings.mqtt_host,
                        self._settings.mqtt_port,
                        self._settings.mqtt_client_id,
                    )
                    for topic in self._subscribed:
                        await client.subscribe(topic, qos=1)
                    await self._notify_connection_change(True, self._session_id)
                    async for message in client.messages:
                        await self._dispatch(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 断连/失败统一走退避重连
                await self._set_disconnected()
                if not self._running:
                    break
                delay = self._backoff.next()
                logger.warning(
                    "Agent MQTT 断连（%s）；%.1fs 后重连（attempt=%d）",
                    exc,
                    delay,
                    self._backoff.attempts,
                )
                await asyncio.sleep(delay)
            finally:
                if self._connected:
                    await self._set_disconnected()

    async def _notify_connection_change(
        self, connected: bool, session_id: str | None
    ) -> None:
        """通知上层连接变化；回调失败不打断 MQTT 循环。"""
        handler = self._connection_handler
        if handler is None:
            return
        try:
            await handler(connected, session_id)
        except Exception:  # noqa: BLE001 - 生命周期回调 fail-open
            logger.exception("Agent MQTT 连接状态回调失败: connected=%s", connected)

    async def _set_disconnected(self) -> None:
        """统一清理断开状态，避免重连路径遗漏回调。"""
        was_connected = self._connected
        self._connected = False
        self._client = None
        if was_connected:
            await self._notify_connection_change(False, self._session_id)

    async def _dispatch(self, raw: Any) -> None:
        """将 aiomqtt 消息转为 MqttMessage 交给处理器（异常不影响循环）。"""
        handler = self._handler
        if handler is None:
            return
        topic = (
            str(raw.topic.value) if hasattr(raw.topic, "value") else str(raw.topic)
        )
        payload = (
            raw.payload if isinstance(raw.payload, bytes) else bytes(raw.payload or b"")
        )
        qos = getattr(raw, "qos", 1)
        try:
            await handler(MqttMessage(topic=topic, payload=payload, qos=qos))
        except Exception:  # noqa: BLE001 - fail open：处理器异常只记录，不中断消费
            logger.exception("Agent MQTT 消息处理失败: topic=%s", topic)
