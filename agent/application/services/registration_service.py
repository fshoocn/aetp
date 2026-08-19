"""Agent 注册与心跳编排（P5.3，§9.7 启动顺序）。

职责：
1. ``register()``：构造 ``node.register`` Envelope 并写入本地 outbox
   （QoS 1 可靠发送），启动注册回执等待任务；
2. ``start_heartbeat()`` / ``stop_heartbeat()``：周期性发布 ``node.heartbeat``；
3. ``handle_register_ack()``：校验消息类型与 sender，记录已接受 session，
   标记注册成功（未收到并校验 register-ack 前不接受 run.assign，§9.7 规则 2）。

本服务只依赖 Transport + Ledger 端口与协议 DTO，不接触具体 MQTT 客户端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from aetp_protocol.capabilities import NodeCapabilities
from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    NodeHeartbeatPayload,
    NodeRegisterPayload,
    RegisterAckPayload,
)
from aetp_protocol.topics import (
    event_topic,
    parse_topic,
    validate_message_type_for_topic,
    validate_sender_for_topic,
)

from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus
from agent.domain.ledger import Ledger
from common.transport import MqttMessage, Transport

if TYPE_CHECKING:
    from agent.plugins import AgentPluginRegistry

logger = logging.getLogger(__name__)


class RegistrationTimeoutError(TimeoutError):
    """在配置的 registration_timeout_s 内没有收到有效 ACK。"""


class RegistrationRejectedError(RuntimeError):
    """Master 拒绝了 Agent 注册。"""


class RegistrationService:
    """节点注册、register-ack 校验与心跳编排。"""

    def __init__(
        self,
        transport: Transport,
        ledger: Ledger,
        settings: AgentSettings,
        *,
        session_id: str | None = None,
        capabilities=None,
        tags: tuple[str, ...] = (),
        plugin_registry: "AgentPluginRegistry | None" = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport
        self._ledger = ledger
        self._settings = settings
        self._session_id = session_id or uuid.uuid4().hex
        self._capabilities = capabilities
        self._tags = list(tags)
        self._plugin_registry = plugin_registry
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._registered = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._register_ack_event = asyncio.Event()
        self._pending_register_message_id: str | None = None
        self._registration_error: RegistrationRejectedError | None = None

    # -- 状态 ---------------------------------------------------------------

    @property
    def registered(self) -> bool:
        """是否已收到并校验 register-ack（§9.7 规则 2）。"""
        return self._registered

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def pending_register_message_id(self) -> str | None:
        """当前注册消息 ID，ACK 必须通过 correlation_id 关联。"""
        return self._pending_register_message_id

    async def handle_connection_change(
        self, connected: bool, session_id: str | None = None
    ) -> None:
        """处理 Transport 连接变化。

        每次底层连接都重新注册；断开时立即撤销 registered 状态并停止心跳，
        防止旧 session 的心跳继续污染 Master 投影。
        """
        if connected:
            await self.stop_heartbeat()
            if session_id:
                self._session_id = session_id
            self._registered = False
            self._registration_error = None
            self._register_ack_event.clear()
            self.enqueue_register()
            return

        self._registered = False
        self._registration_error = None
        self._register_ack_event.clear()
        await self.stop_heartbeat()

    # -- 注册 ---------------------------------------------------------------

    def _build_register_envelope(self) -> Envelope:
        """构造本次注册 Envelope，并由调用方记录 message_id。"""
        payload = self.build_register_payload()
        return Envelope(
            message_id=uuid.uuid4().hex,
            message_type=MessageType.NODE_REGISTER.value,
            sent_at=self._now(),
            sender=Sender(
                kind=SenderKind.AGENT,
                id=self._settings.node_id,
                session_id=self._session_id,
            ),
            trace_id=self._settings.node_id,
            payload=payload.model_dump(mode="json"),
        )

    def build_register_payload(self) -> NodeRegisterPayload:
        """构造注册载荷（节点能力/标签/插件版本来自配置与注册）。"""
        s = self._settings
        # 插件版本由 registry 汇总，不信任手工配置（§9.4）
        plugin_versions: dict[str, str] = {}
        supported_versions: dict[str, list[str]] = {}
        if self._plugin_registry is not None:
            for cap in self._plugin_registry.capabilities():
                plugin_versions[cap.task_type] = cap.plugin_version
                supported_versions[cap.task_type] = sorted(cap.supported_versions)
        return NodeRegisterPayload(
            node_id=s.node_id,
            name=s.name,
            capabilities=(
                self._capabilities
                if self._capabilities is not None
                else NodeCapabilities()
            ),
            tags=self._tags,
            supported_versions=supported_versions,
            plugin_versions=plugin_versions,
        )

    def enqueue_register(self) -> str:
        """构造并写入 node.register Outbox（QoS 1），返回 outbox_id。"""
        envelope = self._build_register_envelope()
        self._pending_register_message_id = envelope.message_id
        topic = event_topic(self._settings.node_id, "register")
        # 注册是可重放的单一逻辑消息；重连时替换旧 session 的 pending/sent 记录，
        # 避免旧注册消息在新 session 之后迟到并反向替换当前会话。
        outbox_id = f"register:{self._settings.node_id}"
        self._ledger.replace_outbox(
            outbox_id, topic, envelope.model_dump(mode="json")
        )
        logger.info("已写入 node.register outbox: %s", outbox_id)
        return outbox_id

    async def publish_register(self) -> None:
        """直接发布 node.register（测试/无 outbox worker 时用）。"""
        envelope = self._build_register_envelope()
        self._pending_register_message_id = envelope.message_id
        topic = event_topic(self._settings.node_id, "register")
        await self._transport.publish(
            topic, json.dumps(envelope.model_dump(mode="json")).encode("utf-8"), qos=1
        )

    async def wait_for_register_ack(self) -> None:
        """等待当前注册消息的有效 ACK，超时或拒绝时抛出明确异常。"""
        try:
            await asyncio.wait_for(
                self._register_ack_event.wait(),
                timeout=self._settings.registration_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise RegistrationTimeoutError(
                f"等待 register-ack 超时: node={self._settings.node_id}"
            ) from exc
        if self._registration_error is not None:
            raise self._registration_error
        if not self._registered:
            raise RegistrationRejectedError("注册未被接受")

    # -- register-ack 校验 --------------------------------------------------

    def handle_register_ack(self, message: MqttMessage) -> bool:
        """校验 register-ack 并标记已注册；无效返回 False。"""
        try:
            topic_info = parse_topic(message.topic)
        except Exception:  # noqa: BLE001 - 非命令主题交由其他路由处理
            return False
        if topic_info.direction != "commands" or topic_info.segment != "register-ack":
            return False
        try:
            envelope = Envelope.model_validate(
                json.loads(message.payload.decode("utf-8"))
            )
            validate_sender_for_topic(message.topic, envelope.sender)
            validate_message_type_for_topic(
                message.topic, MessageType(envelope.message_type)
            )
            payload = RegisterAckPayload.model_validate(envelope.payload)
        except Exception:  # noqa: BLE001 - 非法消息静默忽略
            logger.warning("register-ack 解析失败: topic=%s", message.topic)
            return False
        if envelope.message_type != MessageType.REGISTER_ACK.value:
            return False
        if envelope.sender.id != self._settings.master_id:
            return False
        if (
            topic_info.direction != "commands"
            or topic_info.node_id != self._settings.node_id
        ):
            return False
        if envelope.correlation_id != self._pending_register_message_id:
            return False
        if payload.node_id != self._settings.node_id:
            return False
        if payload.session_id != self._session_id:
            return False
        if not payload.accepted:
            self._registration_error = RegistrationRejectedError(
                payload.reason or "Master 拒绝 Agent 注册"
            )
            self._registered = False
            self._register_ack_event.set()
            return False

        # 记录已接受 session；此后才接受 run.assign（§9.7 规则 2）
        self._registered = True
        self._registration_error = None
        self._register_ack_event.set()
        logger.info(
            "register-ack 校验通过: node=%s session=%s",
            self._settings.node_id,
            self._session_id,
        )
        return True

    # -- 心跳 ---------------------------------------------------------------

    def build_heartbeat_payload(self) -> NodeHeartbeatPayload:
        """构造心跳载荷：从本地账本取真实活动 Run 状态（§8.4 load）。

        ``running_shards`` = 执行中的 Run 数；``queued_shards`` = 已 claim
        待执行；``active_run_ids`` = 活动 run_id 列表（离线恢复现场，§8.6）。
        """
        from agent.domain.enums import AgentRunStatus

        active = self._ledger.list_active_runs()
        running_ids = [
            run.run_id
            for run in active
            if run.status is AgentRunStatus.RUNNING
        ]
        queued_ids = [
            run.run_id
            for run in active
            if run.status is AgentRunStatus.CLAIMED
        ]
        return NodeHeartbeatPayload(
            node_id=self._settings.node_id,
            status="online",
            load={
                "running_shards": len(running_ids),
                "queued_shards": len(queued_ids),
            },
            active_run_ids=[run.run_id for run in active],
        )

    async def publish_heartbeat(self) -> None:
        """发布一次 node.heartbeat（QoS 0，可丢失）。"""
        if not self._registered:
            raise RuntimeError("Agent 尚未收到有效 register-ack，不能发送心跳")
        envelope = Envelope(
            message_id=uuid.uuid4().hex,
            message_type=MessageType.NODE_HEARTBEAT.value,
            sent_at=self._now(),
            sender=Sender(
                kind=SenderKind.AGENT,
                id=self._settings.node_id,
                session_id=self._session_id,
            ),
            trace_id=self._settings.node_id,
            payload=self.build_heartbeat_payload().model_dump(mode="json"),
        )
        topic = event_topic(self._settings.node_id, "heartbeat")
        await self._transport.publish(
            topic, json.dumps(envelope.model_dump(mode="json")).encode("utf-8"), qos=0
        )

    async def start_heartbeat(self) -> None:
        """启动心跳循环（幂等）。"""
        if not self._registered:
            raise RuntimeError("Agent 尚未收到有效 register-ack，不能启动心跳")
        if self._heartbeat_task is not None:
            return
        interval = float(getattr(self._settings, "heartbeat_interval_s", 5))

        async def _loop() -> None:
            while True:
                try:
                    await self.publish_heartbeat()
                except Exception:  # noqa: BLE001 - 心跳失败不退出循环
                    logger.warning("心跳发布失败，稍后重试")
                await asyncio.sleep(interval)

        self._heartbeat_task = asyncio.create_task(_loop())
        logger.info("心跳循环已启动（interval=%ss）", interval)

    async def stop_heartbeat(self) -> None:
        """停止心跳循环（幂等）。"""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
