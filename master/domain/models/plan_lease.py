"""M3 ExecutionPlan 和 ResourceLease 持久化领域记录。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aetp_protocol.execution import ExecutionPlan, ResourceLease


@dataclass(frozen=True)
class ExecutionPlanRecord:
    """不可变 V2 ExecutionPlan 及其内部数据库标识。"""

    id: int | None
    plan: ExecutionPlan
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class ResourceLeaseRecord:
    """可条件更新的 V2 ResourceLease 及其内部数据库标识。"""

    id: int | None
    lease: ResourceLease
    created_at: datetime | None
    updated_at: datetime | None
