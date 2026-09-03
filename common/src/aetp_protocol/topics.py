"""MQTT 主题构建、解析和身份校验。"""

from __future__ import annotations

from dataclasses import dataclass

from .envelope import Sender, SenderKind
from .errors import InvalidSenderError, ProtocolError, TopicMismatchError
from .ids import BusinessId
from .message_types import MessageType, topic_segment_for

PREFIX = "aetp/v2"
_AGENTS_SEG = "agents"
_COMMANDS_SEG = "commands"
_EVENTS_SEG = "events"


@dataclass(frozen=True)
class TopicInfo:
    """解析后的主题信息。"""

    direction: str
    node_id: str
    segment: str
    raw: str


def command_topic(node_id: str, command: str) -> str:
    """构造 Master 到 Agent 的命令主题。"""
    return f"{PREFIX}/master/{_AGENTS_SEG}/{_node_id(node_id)}/{_COMMANDS_SEG}/{command}"


def event_topic(node_id: str, event: str) -> str:
    """构造 Agent 到 Master 的事件主题。"""
    return f"{PREFIX}/{_AGENTS_SEG}/{_node_id(node_id)}/{_EVENTS_SEG}/{event}"


def _node_id(node_id: str) -> str:
    try:
        return BusinessId(node_id).root
    except ValueError as exc:
        raise ValueError("Topic 的 node_id 必须是有效的 ULID BusinessId") from exc


def parse_topic(topic: str) -> TopicInfo:
    """解析当前协议主题，格式错误抛 ProtocolError。"""
    parts = topic.split("/")
    if len(parts) not in (6, 7):
        raise ProtocolError(f"主题格式错误: {topic}")
    if "/".join(parts[:2]) != PREFIX:
        raise ProtocolError(f"主题前缀错误: {topic}")

    if len(parts) == 6:
        _prefix, _version, agents, node_id, direction, segment = parts
    else:
        _prefix, _version, kind, agents, node_id, direction, segment = parts
        if kind != "master":
            raise ProtocolError(f"主题 kind 错误: {topic}")

    if agents != _AGENTS_SEG:
        raise ProtocolError(f"主题 agents 段错误: {topic}")
    if direction not in (_COMMANDS_SEG, _EVENTS_SEG):
        raise ProtocolError(f"主题方向错误: {topic}")
    if not node_id:
        raise ProtocolError(f"主题缺少 node_id: {topic}")
    try:
        typed_node_id = BusinessId(node_id)
    except ValueError as exc:
        raise ProtocolError(f"主题 node_id 不合法: {topic}") from exc
    return TopicInfo(
        direction=direction,
        node_id=typed_node_id.root,
        segment=segment,
        raw=topic,
    )


def validate_sender_for_topic(topic: str, sender: Sender) -> None:
    """校验 sender 与当前主题身份匹配。"""
    info = parse_topic(topic)
    if info.direction == _COMMANDS_SEG:
        if sender.kind != SenderKind.MASTER:
            raise InvalidSenderError(f"commands 主题发送方必须是 master: {topic}")
        return
    if sender.kind != SenderKind.AGENT:
        raise InvalidSenderError(f"events 主题发送方必须是 agent: {topic}")
    if sender.id.root != info.node_id:
        raise InvalidSenderError(
            f"sender.id 与主题 node_id 不匹配: {sender.id.root} != {info.node_id}"
        )


def validate_message_type_for_topic(topic: str, message_type: MessageType) -> None:
    """校验 message_type 与主题段一致。"""
    info = parse_topic(topic)
    expected_direction, expected_segment = topic_segment_for(message_type)
    if info.direction != expected_direction or info.segment != expected_segment:
        raise TopicMismatchError(
            f"message_type {message_type.value} 与主题不匹配: {topic}"
        )


__all__ = [
    "PREFIX",
    "TopicInfo",
    "command_topic",
    "event_topic",
    "parse_topic",
    "validate_message_type_for_topic",
    "validate_sender_for_topic",
]
