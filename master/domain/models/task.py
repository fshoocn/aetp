"""ScriptDefinition 和 TestTask revision 持久化记录。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aetp_protocol.task import ScriptDefinition, TestTask


@dataclass(frozen=True)
class ScriptDefinitionRecord:
    id: int | None
    definition: ScriptDefinition
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class TestTaskRecord:
    id: int | None
    task: TestTask
    created_by: int
    created_at: datetime | None
    updated_at: datetime | None
