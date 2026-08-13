"""P4.4 节点在线投影服务测试（真实 SQLite + 容器）。

验收（§15.3 P4.4）：注册/心跳/LWT 会话校验；旧 session 消息被拒绝；
在线投影（online/status/last_seen_at/load）正确刷新；register-ack 入 outbox。
"""

from __future__ import annotations

import uuid

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    NodeHeartbeatPayload,
    NodeRegisterPayload,
    PresencePayload,
)
from aetp_protocol.topics import command_topic

from master.application.services.node_presence_service import (
    NodePresenceError,
    NodePresenceService,
)
from master.domain.enums import DisconnectReason, NodeStatus, OutboxStatus
from master.domain.time import utcnow


def _uow(container):
    return container.uow_factory()()


def _envelope(node_id: str, session_id: str, message_type: str, **kw) -> Envelope:
    return Envelope(
        protocol_version=1,
        message_id=uuid.uuid4().hex,
        message_type=message_type,
        sent_at=utcnow(),
        sender=Sender(kind=SenderKind.AGENT, id=node_id, session_id=session_id),
        trace_id="trace-1",
        **kw,
    )


def _register(node_id: str = "bench-001", **kw) -> NodeRegisterPayload:
    return NodeRegisterPayload(
        node_id=node_id,
        name="CAN 台架 01",
        capabilities={"can_channels": 2, "canoe": "17"},
        tags=["can", "bench"],
        supported_versions={"can_test": ["1.0"]},
        plugin_versions={"can_test": "1.0"},
        **kw,
    )


def _heartbeat(node_id: str = "bench-001", **kw) -> NodeHeartbeatPayload:
    load = kw.pop("load", {"running_shards": 1, "queued_shards": 0})
    return NodeHeartbeatPayload(
        node_id=node_id,
        status="online",
        load=load,
        **kw,
    )


def _presence(node_id: str = "bench-001", **kw) -> PresencePayload:
    return PresencePayload(node_id=node_id, reason="unexpected_disconnect", **kw)


def _service(container) -> NodePresenceService:
    return NodePresenceService(container.uow_factory(), master_id="aetp-master")


# -- 注册 ----------------------------------------------------------------


def test_register_upserts_node_and_sends_ack(client):
    """注册：upsert 节点（online/能力/tags）、建会话、register-ack 入 outbox。"""
    container = client.app.state.container
    svc = _service(container)
    env = _envelope("bench-001", "sess-1", MessageType.NODE_REGISTER.value)

    ack = svc.handle_register(envelope=env, payload=_register())

    # 节点在线投影
    with _uow(container) as uow:
        node = uow.nodes.get_by_id("bench-001")
        assert node is not None
        assert node.online is True
        assert node.status is NodeStatus.ONLINE
        assert node.capabilities == {"can_channels": 2, "canoe": "17"}
        assert node.tags == ["can", "bench"]
        assert node.last_seen_at is not None
        # 会话已建立
        session = uow.node_sessions.get_current(node.id)
        assert session is not None
        assert session.session_id == "sess-1"

    # register-ack 入 outbox（QoS 1、topic=commands/register-ack、correlation_id=原消息）
    assert ack.aggregate_type == "node"
    assert ack.topic == command_topic("bench-001", "register-ack")
    assert ack.qos == 1
    assert ack.status is OutboxStatus.PENDING
    assert ack.payload["message_type"] == MessageType.REGISTER_ACK.value
    assert ack.payload["correlation_id"] == env.message_id
    assert ack.payload["sender"]["kind"] == "master"
    assert ack.payload["payload"]["node_id"] == "bench-001"
    assert ack.payload["payload"]["session_id"] == "sess-1"
    assert ack.payload["payload"]["accepted"] is True


def test_register_same_session_is_idempotent(client):
    """同 session 重复注册：不重复建会话、仍回 ACK（可重放）。"""
    container = client.app.state.container
    svc = _service(container)
    env = _envelope("bench-001", "sess-1", MessageType.NODE_REGISTER.value)

    svc.handle_register(envelope=env, payload=_register())
    ack2 = svc.handle_register(envelope=env, payload=_register())

    with _uow(container) as uow:
        node = uow.nodes.get_by_id("bench-001")
        assert node is not None
        assert node.online is True
    assert ack2.payload["payload"]["accepted"] is True


def test_register_new_session_replaces_old(client):
    """新 session 注册：旧会话关闭（SESSION_REPLACED），新会话生效。"""
    container = client.app.state.container
    svc = _service(container)

    svc.handle_register(
        envelope=_envelope("bench-001", "sess-old", MessageType.NODE_REGISTER.value),
        payload=_register(),
    )
    svc.handle_register(
        envelope=_envelope("bench-001", "sess-new", MessageType.NODE_REGISTER.value),
        payload=_register(),
    )

    with _uow(container) as uow:
        node = uow.nodes.get_by_id("bench-001")
        assert node is not None
        current = uow.node_sessions.get_current(node.id)
        assert current is not None
        assert current.session_id == "sess-new"
        assert current.disconnected_at is None
        old = uow.node_sessions.get(node.id, "sess-old")
        assert old is not None
        assert old.disconnected_at is not None
        assert old.disconnect_reason is DisconnectReason.SESSION_REPLACED


def test_register_rejects_master_sender(client):
    """sender.kind=master 的注册被拒绝（§8.3 身份校验）。"""
    container = client.app.state.container
    svc = _service(container)
    env = Envelope(
        protocol_version=1,
        message_id=uuid.uuid4().hex,
        message_type=MessageType.NODE_REGISTER.value,
        sent_at=utcnow(),
        sender=Sender(kind=SenderKind.MASTER, id="master", session_id="m-sess"),
        trace_id="t",
    )

    try:
        svc.handle_register(envelope=env, payload=_register())
        raise AssertionError("应拒绝 master sender 的注册")
    except NodePresenceError as exc:
        assert "agent" in str(exc)


def test_register_rejects_sender_id_mismatch(client):
    """sender.id 与 payload.node_id 不一致被拒绝。"""
    container = client.app.state.container
    svc = _service(container)
    env = _envelope("bench-001", "sess-1", MessageType.NODE_REGISTER.value)

    try:
        svc.handle_register(envelope=env, payload=_register(node_id="other"))
        raise AssertionError("应拒绝 sender.id 与 node_id 不一致")
    except NodePresenceError as exc:
        assert "不一致" in str(exc)


# -- 心跳 ----------------------------------------------------------------


def test_heartbeat_refreshes_projection(client):
    """心跳：刷新 online/status/last_seen_at/load（不推进任务终态）。"""
    container = client.app.state.container
    svc = _service(container)
    svc.handle_register(
        envelope=_envelope("bench-001", "sess-1", MessageType.NODE_REGISTER.value),
        payload=_register(),
    )

    svc.handle_heartbeat(
        envelope=_envelope("bench-001", "sess-1", MessageType.NODE_HEARTBEAT.value),
        payload=_heartbeat(load={"running_shards": 2, "queued_shards": 1}),
    )

    with _uow(container) as uow:
        node = uow.nodes.get_by_id("bench-001")
        assert node is not None
        assert node.online is True
        assert node.status is NodeStatus.ONLINE
        assert node.load == {"running_shards": 2, "queued_shards": 1}
        assert node.last_seen_at is not None


def test_heartbeat_old_session_rejected(client):
    """验收：旧 session 的心跳被拒绝（NodePresenceError）。"""
    container = client.app.state.container
    svc = _service(container)
    svc.handle_register(
        envelope=_envelope("bench-001", "sess-old", MessageType.NODE_REGISTER.value),
        payload=_register(),
    )
    # 新会话注册替换旧会话
    svc.handle_register(
        envelope=_envelope("bench-001", "sess-new", MessageType.NODE_REGISTER.value),
        payload=_register(),
    )

    try:
        svc.handle_heartbeat(
            envelope=_envelope(
                "bench-001", "sess-old", MessageType.NODE_HEARTBEAT.value
            ),
            payload=_heartbeat(),
        )
        raise AssertionError("应拒绝旧 session 的心跳")
    except NodePresenceError as exc:
        assert "旧 session" in str(exc)


def test_heartbeat_unregistered_node_rejected(client):
    """未注册节点的心跳被拒绝。"""
    container = client.app.state.container
    svc = _service(container)

    try:
        svc.handle_heartbeat(
            envelope=_envelope("ghost", "sess-1", MessageType.NODE_HEARTBEAT.value),
            payload=_heartbeat(node_id="ghost"),
        )
        raise AssertionError("应拒绝未注册节点的心跳")
    except NodePresenceError as exc:
        assert "未注册" in str(exc)


# -- LWT ----------------------------------------------------------------


def test_presence_marks_node_offline_and_closes_session(client):
    """LWT：关闭当前会话 + 节点投影为 offline。"""
    container = client.app.state.container
    svc = _service(container)
    svc.handle_register(
        envelope=_envelope("bench-001", "sess-1", MessageType.NODE_REGISTER.value),
        payload=_register(),
    )

    svc.handle_presence(
        envelope=_envelope("bench-001", "sess-1", MessageType.PRESENCE.value),
        payload=_presence(),
    )

    with _uow(container) as uow:
        node = uow.nodes.get_by_id("bench-001")
        assert node is not None
        assert node.online is False
        assert node.status is NodeStatus.OFFLINE
        session = uow.node_sessions.get_current(node.id)
        assert session is None  # 已关闭
        old = uow.node_sessions.get(node.id, "sess-1")
        assert old is not None
        assert old.disconnected_at is not None
        assert old.disconnect_reason is DisconnectReason.UNEXPECTED_DISCONNECT


def test_presence_old_session_rejected(client):
    """验收：旧 session 的 LWT 被拒绝。"""
    container = client.app.state.container
    svc = _service(container)
    svc.handle_register(
        envelope=_envelope("bench-001", "sess-old", MessageType.NODE_REGISTER.value),
        payload=_register(),
    )
    svc.handle_register(
        envelope=_envelope("bench-001", "sess-new", MessageType.NODE_REGISTER.value),
        payload=_register(),
    )

    try:
        svc.handle_presence(
            envelope=_envelope("bench-001", "sess-old", MessageType.PRESENCE.value),
            payload=_presence(),
        )
        raise AssertionError("应拒绝旧 session 的 LWT")
    except NodePresenceError as exc:
        assert "旧 session" in str(exc)


def test_presence_unknown_node_ignored(client):
    """未知节点的 LWT 忽略（不抛错、无副作用）。"""
    container = client.app.state.container
    svc = _service(container)
    svc.handle_presence(
        envelope=_envelope("ghost", "sess-1", MessageType.PRESENCE.value),
        payload=_presence(node_id="ghost"),
    )

    with _uow(container) as uow:
        assert uow.nodes.get_by_id("ghost") is None
