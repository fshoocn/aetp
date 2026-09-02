"""Agent 结构化日志领域记录。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aetp_protocol.ids import BusinessId, SessionId
from aetp_protocol.logs import LogEvent


@dataclass(frozen=True)
class AgentLogEventRecord:
    """Master 已接收的一个 Agent 日志事件。"""

    id: int | None
    node_id: BusinessId
    session_id: SessionId
    sequence: int
    event: LogEvent
    batch_first_sequence: int
    received_at: datetime
    created_at: datetime | None
