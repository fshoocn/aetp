"""M2 节点能力快照和诊断快照领域记录。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aetp_protocol.capabilities import NodeCapabilitySnapshot
from aetp_protocol.ids import BusinessId, RequestId, SessionId, Sha256
from aetp_protocol.payloads import DiagnosticsSnapshot


@dataclass(frozen=True)
class NodeCapabilitySnapshotRecord:
    """节点某个 Agent session 的不可变能力快照。"""

    id: int | None
    node_id: BusinessId
    session_id: SessionId
    revision: int
    snapshot_sha256: Sha256
    snapshot: NodeCapabilitySnapshot
    reported_at: datetime
    created_at: datetime | None


@dataclass(frozen=True)
class AgentDiagnosticsSnapshotRecord:
    """节点某次诊断请求的不可变结果。"""

    id: int | None
    request_id: RequestId
    node_id: BusinessId
    session_id: SessionId
    snapshot: DiagnosticsSnapshot
    collected_at: datetime
    created_at: datetime | None
