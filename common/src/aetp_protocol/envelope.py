"""Envelope：统一消息封装与校验（§8.3）。

非法字段（extra=forbid）、协议版本不匹配、未知 message_type、空
message_id/trace_id 均在模型校验时拒绝（P4.1 验收：非法字段被拒绝）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ProtocolError, ProtocolVersionMismatchError
from .message_types import MessageType

# 当前协议版本（整数；收方不支持则拒绝并产生兼容错误，§8.3）
PROTOCOL_VERSION = 1


class SenderKind(StrEnum):
    """消息发送方类型（§8.3）。"""

    MASTER = "master"
    AGENT = "agent"


class Sender(BaseModel):
    """发送方身份（sender.id 必须与 topic/ACL 身份匹配，§8.3）。"""

    model_config = ConfigDict(extra="forbid")

    # sym:kind master / agent
    kind: SenderKind
    # sym:id Master ID 或 node_id（必须与主题身份一致）
    id: str
    # sym:session_id 每次进程启动生成，隔离旧连接（§8.6）
    session_id: str


class Envelope(BaseModel):
    """统一消息信封（§8.3）。"""

    model_config = ConfigDict(extra="forbid")

    # sym:protocol_version 协议版本（整型；不匹配拒绝）
    protocol_version: int = PROTOCOL_VERSION
    # sym:message_id 每次实际 publish 新生成；Inbox 按 sender+message_id 去重
    message_id: str
    # sym:message_type 消息类型（必须为已知 MessageType）
    message_type: str
    # sym:sent_at UTC RFC 3339 时间
    sent_at: datetime
    # sym:sender 发送方身份（kind/id/session_id）
    sender: Sender
    # sym:correlation_id 回执指向触发命令的 message_id（可空）
    correlation_id: str | None = None
    # sym:trace_id 同一任务链路复用（日志/审计关联）
    trace_id: str = ""
    # sym:payload 消息载荷（具体结构见 payloads / §8.4）
    payload: dict = Field(default_factory=dict)

    @field_validator("protocol_version")
    @classmethod
    def _check_protocol_version(cls, value: int) -> int:
        if value != PROTOCOL_VERSION:
            raise ProtocolVersionMismatchError(
                f"协议版本不支持: {value}（期望 {PROTOCOL_VERSION}）"
            )
        return value

    @field_validator("message_type")
    @classmethod
    def _check_message_type(cls, value: str) -> str:
        if value not in MessageType:
            raise ProtocolError(f"未知 message_type: {value}")
        return value

    @field_validator("message_id", "trace_id")
    @classmethod
    def _check_non_empty(cls, value: str) -> str:
        if not value:
            raise ProtocolError("message_id/trace_id 不能为空")
        return value
