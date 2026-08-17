"""Agent 领域枚举（P5.2）。

Agent 本地账本的状态枚举，值与序列化约定与 Master 保持一致
（小写 snake_case 字符串），杜绝魔法字符串。
"""

from __future__ import annotations

from enum import StrEnum


class AgentRunStatus(StrEnum):
    """Agent 本地 Run 执行状态（与 Master 的 RunStatus 投影解耦）。"""

    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class AgentOutboxStatus(StrEnum):
    """Agent 本地出站消息发送状态。"""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    EXHAUSTED = "exhausted"
