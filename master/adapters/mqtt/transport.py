"""MqttTransport：aiomqtt 实现（P4.2，§9.6 阶段 C）。

连接、订阅、发布、TLS、指数退避重连（§9.7 规则 5：重连后恢复订阅）。
实现只依赖 Transport 端口契约 + aiomqtt，业务层不直接接触。
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any, cast

import aiomqtt

from common.backoff import ExponentialBackoff
from common.transport import (
    ConnectionHandler,
    MessageHandler,
    MqttMessage,
    Transport,
    TransportError,
)
from master.config import MasterSettings

logger = logging.getLogger(__name__)


class MqttTransport(Transport):
    """基于 aiomqtt 的 MQTT 传输适配器。

    connect() 启动后台重连循环：每次连接建立后订阅已注册主题并消费
    messages；连接异常/断连时指数退避重连（带抖动），重连后自动恢复订阅。
    """

    def __init__(
        self,
        settings: MasterSettings,
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

    # -- Transport 端口契约 -------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    def on_connection_change(self, handler: ConnectionHandler) -> None:
        """注册连接状态变化处理器。"""
        self._connection_handler = handler

    async def connect(self) -> None:
        """启动后台连接/重连循环（幂等）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("MQTT transport 启动（重连循环）")

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
            logger.info("MQTT 已订阅 %d 个主题", len(topics))

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        if self._client is None or not self._connected:
            raise TransportError(f"MQTT 未连接，无法发布: {topic}")
        await self._client.publish(topic, payload, qos=qos)

    # -- 内部实现 -----------------------------------------------------------

    def _client_kwargs(self) -> dict[str, Any]:
        """构造 aiomqtt.Client 参数（含 TLS）。

        ``clean_start=False``：Master 使用持久会话。进程重启后首次连接不清
        空会话，broker 会在 Master 离线期间缓存 Agent 上报的 QoS 1 事件
        （result/log/register 等），Master 重新上线后补发，避免执行结果丢失。
        """
        s = self._settings
        kwargs: dict[str, Any] = {
            "hostname": s.mqtt_host or "127.0.0.1",
            "port": s.mqtt_port,
            # aiomqtt.Client 使用 identifier 参数；client_id 会导致运行时
            # TypeError，连接循环随后无限重连。
            "identifier": s.mqtt_client_id,
            "keepalive": 30,
            # 持久会话：离线消息由 broker 缓存，重连后补发（§9.7 规则 5）
            "clean_start": False,
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
            ctx.load_verify_locations(cafile=str(self._settings.mqtt_ca_cert_path))
        return ctx

    async def _run_loop(self) -> None:
        """连接 → 订阅 → 消费消息；断连指数退避重连。"""
        while self._running:
            try:
                async with aiomqtt.Client(**self._client_kwargs()) as client:
                    self._client = client
                    self._connected = True
                    await self._notify_connection_change(True, None)
                    self._backoff.reset()
                    logger.info(
                        "MQTT 已连接: %s:%s client=%s",
                        self._settings.mqtt_host,
                        self._settings.mqtt_port,
                        self._settings.mqtt_client_id,
                    )
                    for topic in self._subscribed:
                        await client.subscribe(topic, qos=1)
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
                    "MQTT 断连（%s）；%.1fs 后重连（attempt=%d）",
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
            logger.exception("MQTT 连接状态回调失败: connected=%s", connected)

    async def _set_disconnected(self) -> None:
        """统一清理断开状态，避免重连路径遗漏回调。"""
        was_connected = self._connected
        self._connected = False
        self._client = None
        if was_connected:
            await self._notify_connection_change(False, None)

    async def _dispatch(self, raw: Any) -> None:
        """将 aiomqtt 消息转为 MqttMessage 交给处理器（处理器异常不影响循环）。"""
        handler = self._handler
        if handler is None:
            return
        topic = str(raw.topic.value) if hasattr(raw.topic, "value") else str(raw.topic)
        payload = raw.payload if isinstance(raw.payload, bytes) else bytes(raw.payload or b"")
        qos = getattr(raw, "qos", 1)
        try:
            await handler(MqttMessage(topic=topic, payload=payload, qos=qos))
        except Exception:  # noqa: BLE001 - fail open：处理器异常只记录，不中断消费
            logger.exception("MQTT 消息处理失败: topic=%s", topic)
