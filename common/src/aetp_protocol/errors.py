"""协议错误（P4.1）。

Envelope / topic / sender 校验失败统一抛 ProtocolError 子类，
由 Master/Agent 的 MQTT handler 捕获并拒绝（ACK rejected / 丢弃 + 审计）。
"""

from __future__ import annotations

from pydantic import Field, RootModel


class ErrorCode(RootModel[str]):
    """稳定错误码；业务分支不得依赖诊断 message。"""

    root: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")


V2_ERROR_CODES = frozenset(
    {
        "PROTOCOL_VERSION_UNSUPPORTED",
        "MESSAGE_PAYLOAD_INVALID",
        "PLUGIN_MANIFEST_INVALID",
        "PLUGIN_ENTRYPOINT_INVALID",
        "PLUGIN_PATH_INVALID",
        "PLUGIN_INTEGRITY_INVALID",
        "PLUGIN_VERSION_UNAVAILABLE",
        "PLUGIN_SYNC_FAILED",
        "NODE_CAPABILITY_MISMATCH",
        "AGENT_OFFLINE",
        "AGENT_MAINTENANCE",
        "AGENT_IDENTITY_MISMATCH",
        "SOFTWARE_NOT_FOUND",
        "SOFTWARE_VERSION_MISMATCH",
        "RUNTIME_NOT_FOUND",
        "RESOURCE_UNAVAILABLE",
        "RESOURCE_LEASE_CONFLICT",
        "RESOURCE_LEASE_EXPIRED",
        "RESOURCE_ACTIVATION_FAILED",
        "REQUIREMENT_CONFLICT",
        "EXECUTION_PLAN_INVALID",
        "STALE_SESSION",
        "STALE_ATTEMPT",
        "RETRY_CONFLICT",
        "SCRIPT_CHECKSUM_MISMATCH",
        "ARTIFACT_CHECKSUM_MISMATCH",
        "ARTIFACT_UPLOAD_CONFLICT",
        "ARTIFACT_TOO_LARGE",
        "EXECUTION_RECONCILIATION_REQUIRED",
        "PLUGIN_UI_PROTOCOL_INVALID",
        "AUTHORIZATION_ALLOWED",
        "AUTHORIZATION_DENIED",
        "PROJECT_SCOPE_REQUIRED",
    }
)


class ProtocolError(ValueError):
    """协议校验错误基类。"""


class ProtocolVersionMismatchError(ProtocolError):
    """协议版本不支持。"""


class InvalidSenderError(ProtocolError):
    """sender 与主题/ACL 身份不匹配。"""


class TopicMismatchError(ProtocolError):
    """message_type 与主题段不匹配。"""


class MessagePayloadError(ProtocolError):
    """message_type 对应的 payload 无法通过强类型校验。"""
