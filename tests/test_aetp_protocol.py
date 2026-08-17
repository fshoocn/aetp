"""P4.1：aetp_protocol 共享协议包测试。

验收要点：非法字段被拒绝、错误 topic/sender 被拒绝、协议版本校验、
golden messages 可解析。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aetp_protocol import (
    PROTOCOL_VERSION,
    Envelope,
    MessageType,
    ProtocolError,
    ProtocolVersionMismatchError,
    Sender,
    SenderKind,
    command_topic,
    event_topic,
    parse_topic,
    validate_message_type_for_topic,
    validate_sender_for_topic,
)
from aetp_protocol.golden import (
    GOLDEN_NODE_REGISTER,
    GOLDEN_RUN_ACK,
    GOLDEN_RUN_ASSIGN,
)
from aetp_protocol.payloads import (
    NodeHeartbeatPayload,
    NodeRegisterPayload,
    RunAckPayload,
    RunAssignPayload,
    RunCancelPayload,
)


def _sender(kind: str = "agent", node_id: str = "bench-001") -> Sender:
    return Sender(kind=SenderKind(kind), id=node_id, session_id="019-session")


# ---------------------------------------------------------------------------
# Envelope 解析与非法字段拒绝
# ---------------------------------------------------------------------------


def test_envelope_parses_golden():
    env = Envelope.model_validate(GOLDEN_RUN_ASSIGN)
    assert env.message_type == MessageType.RUN_ASSIGN
    assert env.sender.kind == SenderKind.MASTER
    assert env.sender.id == "master-01"
    assert env.protocol_version == PROTOCOL_VERSION


def test_envelope_rejects_unknown_field():
    """非法字段被拒绝（extra=forbid）。"""
    data = dict(GOLDEN_NODE_REGISTER)
    data["bogus_field"] = 1
    with pytest.raises(ValidationError):
        Envelope.model_validate(data)


def test_envelope_rejects_protocol_version():
    """协议版本不匹配被拒绝（Pydantic 包装为 ValidationError，内部含版本错误）。"""
    data = dict(GOLDEN_NODE_REGISTER)
    data["protocol_version"] = 999
    with pytest.raises(ValidationError, match="协议版本不支持"):
        Envelope.model_validate(data)


def test_envelope_rejects_unknown_message_type():
    """未知 message_type 被拒绝。"""
    data = dict(GOLDEN_NODE_REGISTER)
    data["message_type"] = "foo.bar"
    with pytest.raises(ValidationError, match="未知 message_type"):
        Envelope.model_validate(data)


def test_envelope_rejects_missing_required_field():
    """缺少必填字段（sender）被拒绝。"""
    data = dict(GOLDEN_NODE_REGISTER)
    del data["sender"]
    with pytest.raises(ValidationError):
        Envelope.model_validate(data)


# ---------------------------------------------------------------------------
# topics：构建 / 解析 / sender 身份校验
# ---------------------------------------------------------------------------


def test_topic_build_and_parse():
    assert command_topic("bench-001", "assign") == (
        "aetp/v1/master/agents/bench-001/commands/assign"
    )
    assert event_topic("bench-001", "heartbeat") == (
        "aetp/v1/agents/bench-001/events/heartbeat"
    )
    info = parse_topic("aetp/v1/agents/bench-001/events/result")
    assert info.direction == "events"
    assert info.node_id == "bench-001"
    assert info.segment == "result"


def test_parse_topic_rejects_malformed():
    for bad in (
        "aetp/v1/agents/bench-001/events",        # 段数不足
        "x/v1/agents/bench-001/events/result",    # 前缀错误
        "aetp/v1/agents//events/result",          # node_id 为空
        "aetp/v1/agents/bench-001/bogus/result",  # 方向错误
    ):
        with pytest.raises(ProtocolError):
            parse_topic(bad)


def test_sender_must_match_events_topic():
    """events 主题 sender.kind 必须为 agent 且 sender.id == node_id。"""
    topic = "aetp/v1/agents/bench-001/events/heartbeat"
    validate_sender_for_topic(topic, _sender(kind="agent", node_id="bench-001"))  # OK

    with pytest.raises(ProtocolError, match="发送方必须是 agent"):
        validate_sender_for_topic(topic, _sender(kind="master", node_id="master-01"))
    with pytest.raises(ProtocolError, match="不匹配"):
        validate_sender_for_topic(topic, _sender(kind="agent", node_id="other-node"))


def test_sender_must_match_commands_topic():
    """commands 主题 sender.kind 必须为 master。"""
    topic = "aetp/v1/master/agents/bench-001/commands/assign"
    validate_sender_for_topic(topic, _sender(kind="master", node_id="master-01"))  # OK

    with pytest.raises(ProtocolError, match="发送方必须是 master"):
        validate_sender_for_topic(topic, _sender(kind="agent", node_id="bench-001"))


def test_message_type_must_match_topic():
    """message_type 与主题段不匹配被拒绝（错误 topic 拒绝）。"""
    ok_topic = "aetp/v1/master/agents/bench-001/commands/assign"
    validate_message_type_for_topic(ok_topic, MessageType.RUN_ASSIGN)  # OK

    # 段不匹配：assign 命令主题 + heartbeat 消息
    with pytest.raises(ProtocolError, match="不匹配"):
        validate_message_type_for_topic(ok_topic, MessageType.NODE_HEARTBEAT)
    # 方向不匹配：事件主题 + 命令消息
    with pytest.raises(ProtocolError, match="不匹配"):
        validate_message_type_for_topic(
            "aetp/v1/agents/bench-001/events/register", MessageType.RUN_ASSIGN
        )


# ---------------------------------------------------------------------------
# payloads DTO
# ---------------------------------------------------------------------------


def test_node_register_payload():
    p = NodeRegisterPayload.model_validate(GOLDEN_NODE_REGISTER["payload"])
    assert p.node_id == "bench-001"
    # 强类型层级能力模型：按模型字段/类型访问
    assert p.capabilities.vehicle is not None
    assert p.capabilities.vehicle.vendors[0].buses[0].channels[0].name == "can0"
    assert p.capabilities.system is not None
    assert p.capabilities.system.memory_mb == 16384
    with pytest.raises(ValidationError):
        NodeRegisterPayload.model_validate({"node_id": "x", "bogus": 1})
    # 结构防变形：未知顶层分类/类型错误被模型校验拒绝
    with pytest.raises(ValidationError):
        NodeRegisterPayload.model_validate(
            {
                "node_id": "x",
                "capabilities": {"bogus_category": {"a": 1}},
            }
        )
    with pytest.raises(ValidationError):
        NodeRegisterPayload.model_validate(
            {
                "node_id": "x",
                "capabilities": {"system": {"os": 123}},  # os 应为 str
            }
        )


def test_node_heartbeat_payload():
    p = NodeHeartbeatPayload.model_validate(
        {"node_id": "bench-001", "load": {"running_shards": 1}}
    )
    assert p.status == "online"  # 默认
    assert p.load["running_shards"] == 1


def test_run_assign_payload():
    p = RunAssignPayload.model_validate(GOLDEN_RUN_ASSIGN["payload"])
    assert p.run_id == "R-1"
    assert p.device_allocations == []
    assert p.execution_params == {"channel": 0}
    assert p.script_ref["sha256"] == "a" * 64
    with pytest.raises(ValidationError):
        RunAssignPayload.model_validate({"run_id": "R-1"})  # 缺必填


def test_run_cancel_payload():
    p = RunCancelPayload(run_id="R-1", reason="user requested")
    assert p.run_id == "R-1"
    assert p.reason == "user requested"
    # 默认 reason 为空
    p2 = RunCancelPayload(run_id="R-2")
    assert p2.reason == ""
    with pytest.raises(ValidationError):
        RunCancelPayload.model_validate({"reason": "no run_id"})
    with pytest.raises(ValidationError):
        RunCancelPayload.model_validate({"run_id": "R-1", "bogus": 1})


def test_run_ack_payload():
    p = RunAckPayload.model_validate(GOLDEN_RUN_ACK["payload"])
    assert p.accepted is True
    assert p.dispatch_id == "D-1"
