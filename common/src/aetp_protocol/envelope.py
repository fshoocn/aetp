"""AETP  Envelope 和 typed payload 解析。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from .errors import MessagePayloadError, ProtocolError, ProtocolVersionMismatchError
from .ids import BusinessId, JsonObject, MessageId, SessionId, TraceId
from .message_types import MessageType
from .payloads import PAYLOAD_MODELS

PROTOCOL_VERSION = 2
MESSAGE_TYPES = frozenset(PAYLOAD_MODELS)


class SenderKind(StrEnum):
    MASTER = "master"
    AGENT = "agent"


class Sender(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SenderKind
    id: BusinessId
    session_id: SessionId


class Envelope(BaseModel):
    """ 公共消息头；payload 必须通过 parse_payload 二次解析。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[2] = PROTOCOL_VERSION
    message_id: MessageId
    correlation_id: MessageId | None = None
    sent_at: datetime
    sender: Sender
    message_type: str
    trace_id: TraceId
    payload: JsonObject

    @model_validator(mode="after")
    def validate_message_type(self) -> Envelope:
        if self.message_type not in {message_type.value for message_type in MESSAGE_TYPES}:
            raise ProtocolError(f"未知或非  message_type: {self.message_type}")
        return self

    def parse_payload(self) -> BaseModel:
        """按 message_type 返回唯一严格 payload 模型。"""
        message_type = MessageType(self.message_type)
        payload_model = PAYLOAD_MODELS[message_type]
        try:
            return payload_model.model_validate(self.payload)
        except ValueError as exc:
            raise MessagePayloadError(f"message_type {self.message_type} 的 payload 无效") from exc


def parse_message(data: object) -> tuple[Envelope, BaseModel]:
    """解析 Envelope 并返回已校验的 typed payload。"""
    try:
        envelope = Envelope.model_validate(data)
    except ValueError as exc:
        if "protocol_version" in str(exc):
            raise ProtocolVersionMismatchError("协议版本不支持，期望 2") from exc
        raise
    return envelope, envelope.parse_payload()


Message = tuple[Envelope, BaseModel]
