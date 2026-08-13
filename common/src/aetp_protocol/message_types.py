"""消息类型枚举与 topic 段映射（§8.3/§8.4）。

每个 message_type 对应固定的 topic 方向与段名（events/commands + 段），
用于校验 message_type 与主题一致（P4.1 验收：错误 topic 被拒绝）。
"""

from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    """AETP 协议消息类型（§8.3/§8.4 + verify 扩展）。"""

    # 节点
    NODE_REGISTER = "node.register"          # events/register
    REGISTER_ACK = "register-ack"            # commands/register-ack
    NODE_HEARTBEAT = "node.heartbeat"        # events/heartbeat
    PRESENCE = "presence"                    # events/presence（LWT 非正常离线）
    # 脚本
    SCRIPT_PARSE = "script.parse"            # commands/parse
    SCRIPT_PARSE_RESULT = "script.parse-result"  # events/parse-result
    SCRIPT_VERIFY = "script.verify"          # commands/verify（验证扩展）
    SCRIPT_VERIFY_RESULT = "script.verify-result"  # events/verify-result
    # Run 派发
    RUN_ASSIGN = "run.assign"                # commands/assign
    RUN_CANCEL = "run.cancel"                # commands/cancel
    RUN_ACK = "run.ack"                      # events/ack
    # 运行期
    RUN_PROGRESS = "run.progress"            # events/progress
    RUN_LOG = "run.log"                      # events/log
    RUN_CASE_STATUS = "run.case-status"      # events/case-status
    RUN_RESULT = "run.result"                # events/result
    RUN_LOG_COMPLETE = "run.log-complete"    # events/log-complete


# message_type -> (方向, 段名)；方向：commands（Master→Agent）/ events（Agent→Master）
_MESSAGE_TYPE_TOPIC_SEGMENT: dict[MessageType, tuple[str, str]] = {
    MessageType.NODE_REGISTER: ("events", "register"),
    MessageType.REGISTER_ACK: ("commands", "register-ack"),
    MessageType.NODE_HEARTBEAT: ("events", "heartbeat"),
    MessageType.PRESENCE: ("events", "presence"),
    MessageType.SCRIPT_PARSE: ("commands", "parse"),
    MessageType.SCRIPT_PARSE_RESULT: ("events", "parse-result"),
    MessageType.SCRIPT_VERIFY: ("commands", "verify"),
    MessageType.SCRIPT_VERIFY_RESULT: ("events", "verify-result"),
    MessageType.RUN_ASSIGN: ("commands", "assign"),
    MessageType.RUN_CANCEL: ("commands", "cancel"),
    MessageType.RUN_ACK: ("events", "ack"),
    MessageType.RUN_PROGRESS: ("events", "progress"),
    MessageType.RUN_LOG: ("events", "log"),
    MessageType.RUN_CASE_STATUS: ("events", "case-status"),
    MessageType.RUN_RESULT: ("events", "result"),
    MessageType.RUN_LOG_COMPLETE: ("events", "log-complete"),
}


def topic_segment_for(message_type: MessageType) -> tuple[str, str]:
    """返回 message_type 对应的 (方向, 段名)；未知类型抛 KeyError。"""
    return _MESSAGE_TYPE_TOPIC_SEGMENT[message_type]
