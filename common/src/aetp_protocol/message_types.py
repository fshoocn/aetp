"""消息类型枚举与 topic 段映射（§8.3/§8.4）。

每个 message_type 对应固定的 topic 方向与段名（events/commands + 段），
用于校验 message_type 与主题一致（P4.1 验收：错误 topic 被拒绝）。
"""

from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    """AETP 协议消息类型（§8.3/§8.4 + verify 扩展）。"""

    # 节点
    NODE_REGISTER = "node.register"  # events/register
    REGISTER_ACK = "register-ack"  # commands/register-ack
    NODE_HEARTBEAT = "node.heartbeat"  # events/heartbeat
    PRESENCE = "presence"  # events/presence（LWT 非正常离线）
    # 脚本
    SCRIPT_PARSE = "script.parse"  # commands/parse
    SCRIPT_PARSE_RESULT = "script.parse-result"  # events/parse-result
    SCRIPT_VERIFY = "script.verify"  # commands/verify（验证扩展）
    SCRIPT_VERIFY_RESULT = "script.verify-result"  # events/verify-result
    # Run 派发
    RUN_ASSIGN = "run.assign"  # commands/assign
    RUN_CANCEL = "run.cancel"  # commands/cancel
    RUN_ACK = "run.ack"  # events/ack
    # 运行期
    RUN_PROGRESS = "run.progress"  # events/progress
    RUN_LOG = "run.log"  # events/log
    RUN_CASE_STATUS = "run.case-status"  # events/case-status
    RUN_RESULT = "run.result"  # events/result
    RUN_LOG_COMPLETE = "run.log-complete"  # events/log-complete
    # V2 节点与执行协议
    NODE_REGISTER_ACK = "node.register.ack"
    NODE_CAPABILITY_SNAPSHOT = "node.capability.snapshot"
    EXECUTION_PLAN = "execution.plan"
    EXECUTION_ACK = "execution.ack"
    EXECUTION_CANCEL = "execution.cancel"
    EXECUTION_PROGRESS = "execution.progress"
    EXECUTION_LOG = "execution.log"
    EXECUTION_CASE_STATUS = "execution.case_status"
    EXECUTION_FINISHED = "execution.finished"
    EXECUTION_LOG_COMPLETE = "execution.log_complete"
    LEASE_RENEW = "lease.renew"
    LEASE_RENEWED = "lease.renewed"
    EXECUTION_RECONCILE = "execution.reconcile"
    EXECUTION_RECONCILE_RESULT = "execution.reconcile_result"
    AGENT_PLUGIN_SYNC = "agent.plugin.sync"
    AGENT_PLUGIN_SYNC_RESULT = "agent.plugin.sync.result"
    AGENT_MAINTENANCE_STATUS = "agent.maintenance.status"
    AGENT_DIAGNOSTICS_REQUEST = "agent.diagnostics.request"
    AGENT_DIAGNOSTICS_SNAPSHOT = "agent.diagnostics.snapshot"
    AGENT_LOG_BATCH = "agent.log.batch"
    AGENT_LOG_RECEIVED = "agent.log.received"


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
    MessageType.NODE_REGISTER_ACK: ("commands", "register.ack"),
    MessageType.NODE_CAPABILITY_SNAPSHOT: ("events", "capability.snapshot"),
    MessageType.EXECUTION_PLAN: ("commands", "execution.plan"),
    MessageType.EXECUTION_ACK: ("events", "execution.ack"),
    MessageType.EXECUTION_CANCEL: ("commands", "execution.cancel"),
    MessageType.EXECUTION_PROGRESS: ("events", "execution.progress"),
    MessageType.EXECUTION_LOG: ("events", "execution.log"),
    MessageType.EXECUTION_CASE_STATUS: ("events", "execution.case_status"),
    MessageType.EXECUTION_FINISHED: ("events", "execution.finished"),
    MessageType.EXECUTION_LOG_COMPLETE: ("events", "execution.log_complete"),
    MessageType.LEASE_RENEW: ("events", "lease.renew"),
    MessageType.LEASE_RENEWED: ("commands", "lease.renewed"),
    MessageType.EXECUTION_RECONCILE: ("events", "execution.reconcile"),
    MessageType.EXECUTION_RECONCILE_RESULT: ("commands", "execution.reconcile_result"),
    MessageType.AGENT_PLUGIN_SYNC: ("commands", "agent.plugin.sync"),
    MessageType.AGENT_PLUGIN_SYNC_RESULT: ("events", "agent.plugin.sync.result"),
    MessageType.AGENT_MAINTENANCE_STATUS: ("events", "agent.maintenance.status"),
    MessageType.AGENT_DIAGNOSTICS_REQUEST: ("commands", "agent.diagnostics.request"),
    MessageType.AGENT_DIAGNOSTICS_SNAPSHOT: ("events", "agent.diagnostics.snapshot"),
    MessageType.AGENT_LOG_BATCH: ("events", "agent.log.batch"),
    MessageType.AGENT_LOG_RECEIVED: ("commands", "agent.log.received"),
}


def topic_segment_for(message_type: MessageType) -> tuple[str, str]:
    """返回 message_type 对应的 (方向, 段名)；未知类型抛 KeyError。"""
    return _MESSAGE_TYPE_TOPIC_SEGMENT[message_type]
