"""节点注册、心跳和 Presence 在线投影。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aetp_protocol.capabilities import NodeCapabilities
from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.ids import MessageId, SessionId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import Heartbeat, NodeRegister, NodeRegisterAck, Presence
from aetp_protocol.topics import command_topic

from master.application.services.recovery_service import RecoveryService
from master.domain.enums import DisconnectReason, NodeStatus, OutboxStatus
from master.domain.models import Node, NodeSession, OutboxMessage
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)


class NodePresenceError(Exception):
    """节点在线投影业务错误。"""


class NodePresenceService:
    """节点注册、心跳、Presence 和会话切换处理。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        master_id: str = "aetp-master",
        recovery_service: RecoveryService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._master_id = master_id
        self._recovery = recovery_service

    def handle_register(self, *, envelope: Envelope, payload: NodeRegister) -> OutboxMessage:
        """注册节点并写入当前协议注册回执。"""
        if envelope.sender.kind != "agent":
            raise NodePresenceError("注册 sender.kind 必须为 agent")
        if envelope.sender.id != payload.node_id:
            raise NodePresenceError("注册 sender.id 与 node_id 不一致")
        if envelope.sender.session_id != payload.session_id:
            raise NodePresenceError("注册 sender.session_id 与 payload.session_id 不一致")
        snapshot = payload.capability_snapshot
        if snapshot.node_id != payload.node_id or snapshot.session_id != payload.session_id:
            raise NodePresenceError("注册能力快照身份与注册载荷不一致")

        now = utcnow()
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(payload.node_id.root)
            if node is None:
                node = Node(
                    id=None,
                    node_id=payload.node_id.root,
                    name=payload.name,
                    hostname="",
                    status=NodeStatus.ONLINE,
                    online=True,
                    enabled=True,
                    tags=list(payload.tags),
                    capabilities=NodeCapabilities(),
                    protocol_version=str(envelope.protocol_version),
                    last_seen_at=now,
                    load={},
                )
            else:
                node.name = payload.name or node.name
                node.status = NodeStatus.ONLINE if node.enabled else NodeStatus.DISABLED
                node.online = True
                node.tags = list(payload.tags)
                node.protocol_version = str(envelope.protocol_version)
                node.last_seen_at = now
            node.resource_occupancy = {}
            node = uow.nodes.save(node)
            assert node.id is not None

            current = uow.node_sessions.get_current(node.id)
            if current is not None and current.session_id != payload.session_id.root:
                uow.node_sessions.close(
                    current,
                    reason=DisconnectReason.SESSION_REPLACED,
                    at=now,
                )

            existing = uow.node_sessions.get(node.id, payload.session_id.root)
            if existing is None:
                uow.node_sessions.create(
                    NodeSession(
                        node_pk=node.id,
                        node_id=node.node_id,
                        session_id=payload.session_id.root,
                        client_id=payload.session_id.root,
                        connected_at=now,
                    )
                )

            outbox_id = stable_id(f"register-ack:{envelope.message_id.root}").root
            existing_ack = uow.outbox_messages.get_by_outbox_id(outbox_id)
            if existing_ack is not None:
                return existing_ack
            ack = uow.outbox_messages.enqueue(
                OutboxMessage(
                    outbox_id=outbox_id,
                    aggregate_type="node",
                    aggregate_id=node.node_id,
                    topic=command_topic(node.node_id, "register.ack"),
                    payload=self._build_ack_envelope(envelope, payload).model_dump(mode="json"),
                    qos=1,
                    status=OutboxStatus.PENDING,
                    attempts=0,
                    next_attempt_at=None,
                )
            )
            logger.info(
                "节点注册成功: node=%s session=%s → node.register.ack 入 outbox",
                node.node_id,
                payload.session_id.root,
            )
            return ack

    def handle_heartbeat(self, *, envelope: Envelope, payload: Heartbeat) -> None:
        """按当前会话刷新节点在线投影。"""
        self._validate_sender(envelope, payload.node_id)
        now = utcnow()
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(payload.node_id.root)
            if node is None:
                raise NodePresenceError(f"未注册节点心跳被拒: {payload.node_id.root}")
            assert node.id is not None
            current = uow.node_sessions.get_current(node.id)
            if current is None or current.session_id != envelope.sender.session_id.root:
                raise NodePresenceError(
                    f"旧 session 消息被拒绝: node={payload.node_id.root} "
                    f"session={envelope.sender.session_id.root}"
                )
            node.online = payload.status.value == "online"
            node.status = NodeStatus.ONLINE if node.online and node.enabled else NodeStatus.OFFLINE
            node.last_seen_at = now
            node.load = {
                "running_attempts": payload.load.running_attempts,
                "queued_attempts": payload.load.queued_attempts,
                "maintenance_state": payload.maintenance_state.value,
                "capability_revision": payload.capability_revision,
            }
            uow.nodes.save(node)

    def handle_presence(self, *, envelope: Envelope, payload: Presence) -> None:
        """关闭当前会话并触发节点离线恢复。"""
        self._validate_sender(envelope, payload.node_id)
        now = utcnow()
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(payload.node_id.root)
            if node is None:
                logger.warning("未知节点 Presence 忽略: %s", payload.node_id.root)
                return
            assert node.id is not None
            current = uow.node_sessions.get_current(node.id)
            if current is not None:
                if current.session_id != envelope.sender.session_id.root:
                    raise NodePresenceError(
                        f"旧 session 消息被拒绝: node={payload.node_id.root} "
                        f"session={envelope.sender.session_id.root}"
                    )
                uow.node_sessions.close(
                    current,
                    reason=DisconnectReason.UNEXPECTED_DISCONNECT,
                    at=now,
                )
            node.online = False
            node.status = NodeStatus.OFFLINE
            node.last_seen_at = now
            uow.nodes.save(node)

        if self._recovery is not None:
            try:
                self._recovery.handle_node_offline(payload.node_id.root)
            except Exception:
                logger.exception("节点离线恢复失败: node=%s", payload.node_id.root)

    @staticmethod
    def _validate_sender(envelope: Envelope, node_id) -> None:
        if envelope.sender.kind != "agent":
            raise NodePresenceError("sender.kind 必须为 agent")
        if envelope.sender.id != node_id:
            raise NodePresenceError("sender.id 与 node_id 不一致")

    def _build_ack_envelope(self, request: Envelope, payload: NodeRegister) -> Envelope:
        """构造注册回执，correlation_id 指向原注册消息。"""
        return Envelope(
            message_id=MessageId(new_id()),
            correlation_id=request.message_id,
            sent_at=utcnow(),
            sender=Sender(
                kind=SenderKind.MASTER,
                id=stable_id(self._master_id),
                session_id=SessionId(stable_id(f"{self._master_id}:session").root),
            ),
            message_type=MessageType.NODE_REGISTER_ACK.value,
            trace_id=request.trace_id,
            payload=NodeRegisterAck(
                node_id=payload.node_id,
                session_id=payload.session_id,
                accepted=True,
            ).model_dump(mode="json"),
        )
