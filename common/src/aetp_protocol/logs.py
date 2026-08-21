"""共享任务日志 DTO（P6.2，§9.4）。

``RunLogEntry`` / ``RunLogBatch`` 是 Master 与 Agent 共用的严格 Pydantic 模型：

- ``RunLogEntry``：单条任务日志（``(run_id, sequence)`` 在接收端幂等去重）；
- ``RunLogBatch``：批量日志上报信封（``entries`` 严格按 sequence 递增，
  ``first_sequence`` 等于首条 sequence）。

约束（§9.4）：

1. ``entries`` 必须按 sequence 严格递增，``first_sequence`` 等于首条 sequence；
2. ``detail`` 只允许 JSON 基础类型、列表和对象；
3. 单条日志 message 长度 1..8192。

本模块只定义「消息是什么」，不定义「收到后怎样写库/执行」（§4.6）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LogLevel(StrEnum):
    """任务日志等级（小写，与 Master/Agent 序列化约定一致）。"""

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class RunLogEntry(BaseModel):
    """单条任务日志（§9.4）。"""

    model_config = ConfigDict(extra="forbid")

    # sym:project_id 项目业务标识
    project_id: str
    # sym:task_id 任务定义业务标识
    task_id: str
    # sym:run_id 所属 Run
    run_id: str
    # sym:node_id 产生日志的 Agent 节点
    node_id: str
    # sym:sequence Run 内单调递增序号（ge=1）
    sequence: int = Field(ge=1)
    # sym:level 日志等级
    level: LogLevel
    # sym:message 日志正文（1..8192）
    message: str = Field(min_length=1, max_length=8_192)
    # sym:detail 结构化详情（仅 JSON 基础类型/列表/对象）
    detail: dict[str, Any] = Field(default_factory=dict)
    # sym:occurred_at 产生时间（UTC）
    occurred_at: datetime


class RunLogBatch(BaseModel):
    """批量日志上报信封（§9.4）。"""

    model_config = ConfigDict(extra="forbid")

    # sym:run_id 所属 Run
    run_id: str
    # sym:first_sequence 首条 sequence（ge=1）
    first_sequence: int = Field(ge=1)
    # sym:entries 日志条目（1..50，严格递增）
    entries: list[RunLogEntry] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _check_sequences(self) -> RunLogBatch:
        """entries 必须严格按 sequence 递增，first_sequence 等于首条。"""
        if self.entries:
            if self.entries[0].sequence != self.first_sequence:
                raise ValueError(
                    f"first_sequence 必须等于首条 entry.sequence: {self.first_sequence} != {self.entries[0].sequence}"
                )
            for prev, cur in zip(self.entries, self.entries[1:], strict=False):
                if cur.sequence <= prev.sequence:
                    raise ValueError(f"entries 必须按 sequence 严格递增: {prev.sequence} -> {cur.sequence}")
        return self
