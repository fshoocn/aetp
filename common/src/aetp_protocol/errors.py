"""协议错误（P4.1）。

Envelope / topic / sender 校验失败统一抛 ProtocolError 子类，
由 Master/Agent 的 MQTT handler 捕获并拒绝（ACK rejected / 丢弃 + 审计）。
"""

from __future__ import annotations


class ProtocolError(ValueError):
    """协议校验错误基类。"""


class ProtocolVersionMismatchError(ProtocolError):
    """协议版本不支持。"""


class InvalidSenderError(ProtocolError):
    """sender 与主题/ACL 身份不匹配。"""


class TopicMismatchError(ProtocolError):
    """message_type 与主题段不匹配。"""
