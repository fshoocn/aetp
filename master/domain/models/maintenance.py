"""Agent 远程运维操作和节点维护锁领域记录。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aetp_protocol.ids import BusinessId, SessionId
from aetp_protocol.payloads import RemoteOperationStatus


@dataclass(frozen=True)
class RemoteOperationRecord:
    id: int | None
    operation_id: BusinessId
    node_id: BusinessId
    kind: str
    status: RemoteOperationStatus
    expected_session_id: SessionId | None
    request: dict
    error_code: str | None
    message: str
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class NodeMaintenanceLockRecord:
    id: int | None
    node_id: BusinessId
    operation_id: BusinessId
    kind: str
    acquired_at: datetime | None
