"""节点在线投影服务（P4.4，§8.6 / §9.7 规则 2）。

处理 node.register / node.heartbeat / presence（LWT）：
- 注册：校验 sender 身份 → 关闭旧 session（SESSION_REPLACED）→ upsert 节点 →
  建新 session → outbox 回 register-ack（QoS 1，§8.2）
- 心跳：校验当前会话 → 刷新 online / last_seen_at / load
  （只刷新投影，不推进任务终态，§8.4）
- LWT：校验当前会话 → 关闭会话 → 节点 offline（§8.6）

会话校验（P4.4 验收：旧 session 消息被拒绝）：节点当前有效会话与
envelope.sender.session_id 不一致时拒绝；重复注册（同 session）幂等重放 ACK。
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    NodeHeartbeatPayload,
    NodeRegisterPayload,
    PresencePayload,
    RegisterAckPayload,
)
from aetp_protocol.topics import command_topic

from master.domain.enums import DisconnectReason, NodeStatus, OutboxStatus
from master.domain.models import Node, NodeSession, OutboxMessage
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)


class NodePresenceError(Exception):
    """节点在线投影业务错误（会话校验失败 / 未注册节点 / sender 身份不符）。"""


class NodePresenceService:
    """节点注册 / 心跳 / LWT 处理与在线投影（纯 UoW 依赖，可单测）。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        master_id: str = "aetp-master",
    ) -> None:
        self._uow_factory = uow_factory
        self._master_id = master_id

    # -- 注册 ---------------------------------------------------------------

    def handle_register(
        self, *, envelope: Envelope, payload: NodeRegisterPayload
    ) -> OutboxMessage:
        """节点注册：upsert 节点 + 会话切换 + outbox 回 register-ack。"""
        self._validate_agent_sender(envelope, payload.node_id)
        now = utcnow()
        # 领域层保持 NodeCapabilities；JSON 序列化只发生在仓储边界
        caps = payload.capabilities

        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(payload.node_id)
            if node is None:
                node = Node(
                    id=None,
                    node_id=payload.node_id,
                    name=payload.name,
                    hostname="",
                    status=NodeStatus.ONLINE,
                    online=True,
                    enabled=True,
                    tags=payload.tags,
                    capabilities=caps,
                    protocol_version=str(envelope.protocol_version),
                    last_seen_at=now,
                    load={},
                )
            else:
                node.name = payload.name or node.name
                node.status = NodeStatus.ONLINE
                node.online = True
                node.tags = payload.tags
                node.capabilities = caps
                node.protocol_version = str(envelope.protocol_version)
                node.last_seen_at = now
            node = uow.nodes.save(node)
            assert node.id is not None  # save 后必有代理主键（后续会话查询用）

            # 关闭旧会话（同节点不同 session_id → SESSION_REPLACED）
            current = uow.node_sessions.get_current(node.id)
            if (
                current is not None
                and current.session_id != envelope.sender.session_id
            ):
                uow.node_sessions.close(
                    current,
                    reason=DisconnectReason.SESSION_REPLACED,
                    at=now,
                )
                logger.info(
                    "节点 %s 旧会话被替换: %s → %s",
                    node.node_id,
                    current.session_id,
                    envelope.sender.session_id,
                )

            # 新建会话（同 session 重复注册幂等，不重复建）
            existing = uow.node_sessions.get(node.id, envelope.sender.session_id)
            if existing is None:
                uow.node_sessions.create(
                    NodeSession(
                        node_pk=node.id,
                        node_id=node.node_id,
                        session_id=envelope.sender.session_id,
                        client_id=envelope.sender.session_id,
                        connected_at=now,
                    )
                )

            # register-ack（事务性 outbox，QoS 1；同 session 重放相同 ACK 语义）
            ack = uow.outbox_messages.enqueue(
                OutboxMessage(
                    outbox_id=uuid.uuid4().hex,
                    aggregate_type="node",
                    aggregate_id=node.node_id,
                    topic=command_topic(node.node_id, "register-ack"),
                    payload=self._build_ack_envelope(envelope, node.node_id),
                    qos=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=None,
                )
            )
            logger.info(
                "节点注册成功: node=%s session=%s → register-ack 入 outbox",
                node.node_id,
                envelope.sender.session_id,
            )
            return ack

    # -- 心跳 ---------------------------------------------------------------

    def handle_heartbeat(
        self, *, envelope: Envelope, payload: NodeHeartbeatPayload
    ) -> None:
        """心跳：只刷新在线投影（会话校验失败抛 NodePresenceError）。"""
        self._validate_agent_sender(envelope, payload.node_id)
        now = utcnow()

        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(payload.node_id)
            if node is None:
                raise NodePresenceError(
                    f"未注册节点心跳被拒: {payload.node_id}"
                )
            assert node.id is not None  # 已存在节点必有代理主键
            current = uow.node_sessions.get_current(node.id)
            if current is None or current.session_id != envelope.sender.session_id:
                raise NodePresenceError(
                    f"旧 session 消息被拒绝: node={payload.node_id} "
                    f"session={envelope.sender.session_id}"
                )
            node.online = True
            node.status = NodeStatus.ONLINE if node.enabled else NodeStatus.DISABLED
            node.last_seen_at = now
            node.load = payload.load
            uow.nodes.save(node)
            logger.debug(
                "节点心跳: node=%s load=%s", payload.node_id, payload.load
            )

    # -- LWT（非正常离线） ----------------------------------------------------

    def handle_presence(
        self, *, envelope: Envelope, payload: PresencePayload
    ) -> None:
        """LWT：关闭当前会话并把节点投影为 offline（§8.6 步骤 1-2）。"""
        self._validate_agent_sender(envelope, payload.node_id)
        now = utcnow()

        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(payload.node_id)
            if node is None:
                logger.warning("未知节点 LWT 忽略: %s", payload.node_id)
                return
            assert node.id is not None  # 已存在节点必有代理主键
            current = uow.node_sessions.get_current(node.id)
            if current is not None:
                if current.session_id != envelope.sender.session_id:
                    raise NodePresenceError(
                        f"旧 session 消息被拒绝: node={payload.node_id} "
                        f"session={envelope.sender.session_id}"
                    )
                reason = DisconnectReason(payload.reason or "unexpected_disconnect")
                uow.node_sessions.close(current, reason=reason, at=now)
            node.online = False
            node.status = NodeStatus.OFFLINE
            node.last_seen_at = now
            uow.nodes.save(node)
            logger.info(
                "节点 LWT 离线: node=%s session=%s reason=%s",
                payload.node_id,
                envelope.sender.session_id,
                payload.reason,
            )

    # -- 内部 ---------------------------------------------------------------

    def _validate_agent_sender(self, envelope: Envelope, node_id: str) -> None:
        """sender 身份校验（§8.3：agent 事件必须 sender.kind=agent 且 id==node_id）。"""
        if envelope.sender.kind is not SenderKind.AGENT:
            raise NodePresenceError(
                f"sender.kind 必须为 agent: {envelope.sender.kind}"
            )
        if envelope.sender.id != node_id:
            raise NodePresenceError(
                f"sender.id 与 node_id 不一致: {envelope.sender.id} != {node_id}"
            )

    def _build_ack_envelope(self, request: Envelope, node_id: str) -> dict:
        """构造 register-ack 的 Envelope JSON（sender=master，correlation_id=原消息）。"""
        ack = Envelope(
            protocol_version=1,
            message_id=uuid.uuid4().hex,
            message_type=MessageType.REGISTER_ACK.value,
            sent_at=utcnow(),
            sender=Sender(
                kind=SenderKind.MASTER,
                id=self._master_id,
                session_id=self._master_id,
            ),
            correlation_id=request.message_id,
            trace_id=request.trace_id,
            payload=RegisterAckPayload(
                node_id=node_id,
                session_id=request.sender.session_id,
                accepted=True,
            ).model_dump(mode="json"),
        )
        return ack.model_dump(mode="json")
