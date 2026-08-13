"""AETP 共享协议包（P4.1，D-01）。

独立、可安装、可版本化的 Python 包（common/src/aetp_protocol），
Master / Agent / 测试工具 / CLI 复用同一契约（§4.6）。

只定义“消息是什么”，不定义“收到后怎样写库/执行/通知”：
- envelope：统一消息信封与校验（非法字段/未知类型/版本拒绝）
- message_types：消息类型枚举 + 类型↔topic 段映射
- topics：主题构建/解析/sender 身份校验（错误 topic/sender 拒绝）
- payloads：关键消息载荷 DTO
- golden：golden messages 样例（契约测试数据源）

允许：标准库、Pydantic、不可变 DTO；禁止：FastAPI/SQLAlchemy/aiomqtt/
文件路径/环境变量/线程事件循环（§4.6）。
"""

from __future__ import annotations

from .envelope import PROTOCOL_VERSION, Envelope, Sender, SenderKind
from .errors import (
    InvalidSenderError,
    ProtocolError,
    ProtocolVersionMismatchError,
    TopicMismatchError,
)
from .message_types import MessageType
from .topics import TopicInfo, command_topic, event_topic, parse_topic
from .topics import validate_message_type_for_topic, validate_sender_for_topic

__all__ = [
    "PROTOCOL_VERSION",
    "Envelope",
    "Sender",
    "SenderKind",
    "MessageType",
    "TopicInfo",
    "command_topic",
    "event_topic",
    "parse_topic",
    "validate_sender_for_topic",
    "validate_message_type_for_topic",
    "ProtocolError",
    "ProtocolVersionMismatchError",
    "InvalidSenderError",
    "TopicMismatchError",
]
