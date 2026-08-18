"""领域对象：Run 执行日志（run_logs 表）。

Run 级日志（§9.4），由 Agent 的 ``run.log``（RunLogBatch）上报；按
(run_id, sequence) 幂等去重，日志围栏（run.log-complete）后拒绝新条目。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from master.domain.enums import RunLogLevel


@dataclass
class RunLog:
    """Run 执行日志行。"""

    id: int | None = None
    run_id: str = ""
    shard_id: str | None = None
    node_id: str = ""
    sequence: int = 0
    level: RunLogLevel = RunLogLevel.INFO
    message: str = ""
    detail: dict | None = None
    occurred_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# 防止 pytest 将 RunLog 误识别为测试类。
setattr(RunLog, "__test__", False)
