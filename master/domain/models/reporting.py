"""Reporter/Analyzer 执行结果领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.time import utcnow


@dataclass
class RunExtensionResult:
    """一次 Run 扩展处理的持久化结果。"""

    id: int | None = None
    extension_id: str = ""
    run_id: str = ""
    extension_point: str = ""
    plugin_id: str = ""
    plugin_version: str = ""
    status: str = "succeeded"
    result: dict | None = None
    derived_artifact_ids: list[str] = field(default_factory=list)
    error_message: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
