"""MQTT 主题函数与身份校验（§8.2/§8.3）。

- 主题构建：commands（Master→Agent）/ events（Agent→Master）
- 主题解析：TopicInfo（方向、node_id、段名）
- sender 身份校验：commands 主题 sender 必须为 master；events 主题 sender
  必须为 agent 且 sender.id == topic.node_id（错误 topic/sender 被拒绝，P4.1）
- message_type 与主题段匹配校验
"""

from __future__ import annotations

from dataclasses import dataclass

from .envelope import Sender, SenderKind
from .errors import InvalidSenderError, ProtocolError, TopicMismatchError
from .message_types import MessageType, topic_segment_for

PREFIX = "aetp/v1"
_AGENTS_SEG = "agents"
_COMMANDS_SEG = "commands"
_EVENTS_SEG = "events"


@dataclass(frozen=True)
class TopicInfo:
    """解析后的主题信息。"""

    # sym:direction 方向：commands（Master→Agent）/ events（Agent→Master）
    direction: str
    # sym:node_id 目标/来源节点业务 ID
    node_id: str
    # sym:segment 命令/事件段名（register/heartbeat/assign/...）
    segment: str
    # sym:raw 原始主题串
    raw: str


def command_topic(node_id: str, command: str) -> str:
    """Master→Agent 命令主题（aetp/v1/master/agents/{node_id}/commands/{cmd}）。"""
    return f"{PREFIX}/master/{_AGENTS_SEG}/{node_id}/{_COMMANDS_SEG}/{command}"


def event_topic(node_id: str, event: str) -> str:
    """Agent→Master 事件主题（aetp/v1/agents/{node_id}/events/{event}）。"""
    return f"{PREFIX}/{_AGENTS_SEG}/{node_id}/{_EVENTS_SEG}/{event}"


def parse_topic(topic: str) -> TopicInfo:
    """解析主题；格式错误抛 ProtocolError。

    支持两种形态：
    - events（Agent→Master）：aetp/v1/agents/{node_id}/events/{segment}  （6 段）
    - commands（Master→Agent）：aetp/v1/master/agents/{node_id}/commands/{segment}  （7 段）
    """
    parts = topic.split("/")
    if len(parts) not in (6, 7):
        raise ProtocolError(f"主题格式错误: {topic}")
    if "/".join(parts[:2]) != PREFIX:
        raise ProtocolError(f"主题前缀错误: {topic}")

    if len(parts) == 6:
        # aetp / v1 / agents / {node_id} / events / {segment}
        _prefix, _v, agents, node_id, direction, segment = parts
    else:
        # aetp / v1 / master / agents / {node_id} / commands / {segment}
        _prefix, _v, kind, agents, node_id, direction, segment = parts
        if kind != "master":
            raise ProtocolError(f"主题 kind 错误: {topic}")

    if agents != _AGENTS_SEG:
        raise ProtocolError(f"主题 agents 段错误: {topic}")
    if direction not in (_COMMANDS_SEG, _EVENTS_SEG):
        raise ProtocolError(f"主题方向错误: {topic}")
    if not node_id:
        raise ProtocolError(f"主题缺少 node_id: {topic}")
    return TopicInfo(direction=direction, node_id=node_id, segment=segment, raw=topic)


def validate_sender_for_topic(topic: str, sender: Sender) -> None:
    """校验 sender 与主题身份匹配（§8.3：sender.id 必须与 topic/ACL 身份匹配）。

    - commands 主题（Master→Agent）：sender.kind 必须为 master
    - events 主题（Agent→Master）：sender.kind 必须为 agent 且 sender.id == node_id
    """
    info = parse_topic(topic)
    if info.direction == _COMMANDS_SEG:
        if sender.kind != SenderKind.MASTER:
            raise InvalidSenderError(f"commands 主题发送方必须是 master: {topic}（sender.kind={sender.kind}）")
    else:
        if sender.kind != SenderKind.AGENT:
            raise InvalidSenderError(f"events 主题发送方必须是 agent: {topic}（sender.kind={sender.kind}）")
        if sender.id != info.node_id:
            raise InvalidSenderError(f"sender.id 与主题 node_id 不匹配: {sender.id} != {info.node_id}（topic={topic}）")


def validate_message_type_for_topic(topic: str, message_type: MessageType) -> None:
    """校验 message_type 与主题段一致（P4.1：错误 topic 被拒绝）。"""
    info = parse_topic(topic)
    expected_direction, expected_segment = topic_segment_for(message_type)
    if info.direction != expected_direction or info.segment != expected_segment:
        raise TopicMismatchError(
            f"message_type {message_type.value} 与主题不匹配: {topic}（期望 {expected_direction}/{expected_segment}）"
        )
