"""Agent 本地账本端口与领域模型（P5.2，§9.3）。

Agent 在本地 SQLite 保存执行相关状态，Master 不感知：

- ``AgentRun``：run claim 与执行状态（run_id 主键，claim 原子）
- ``AgentInboxEntry``：入站命令去重（(origin_id, message_id) 唯一）
- ``AgentOutboxEntry``：注册/ACK/结果可靠重发
- ``TaskLogSpoolEntry``：任务日志本地缓冲
- ``ScriptCacheEntry``：脚本包本地缓存（按 hash 去重）

本模块只定义端口与数据对象；实现见 ``agent/adapters/sqlite/ledger.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from agent.domain.enums import AgentOutboxStatus, AgentRunStatus


@dataclass
class AgentRun:
    """Agent 本地的一次 Run claim 与执行状态。"""

    run_id: str
    attempt_no: int
    status: AgentRunStatus = AgentRunStatus.CLAIMED
    cancelled: bool = False
    result_summary: dict = field(default_factory=dict)
    # sym:device_ids 本次 Run 占用的物理设备集合（来自 run.assign.device_allocations）
    # 用于心跳汇总「谁占了哪个口」并上报给 Master（§9.8 占用状态）。
    device_ids: list[str] = field(default_factory=list)
    claimed_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class AgentInboxEntry:
    """入站命令去重记录。"""

    origin_id: str
    message_id: str
    message_type: str
    received_at: datetime | None = None


@dataclass
class AgentOutboxEntry:
    """待可靠发送的出站消息。"""

    outbox_id: str
    topic: str
    payload: dict = field(default_factory=dict)
    status: AgentOutboxStatus = AgentOutboxStatus.PENDING
    attempts: int = 0
    next_attempt_at: datetime | None = None
    claimed_until: datetime | None = None


@dataclass
class TaskLogSpoolEntry:
    """任务日志本地缓冲条目。"""

    run_id: str
    sequence: int
    level: str
    message: str
    detail: dict = field(default_factory=dict)
    published: bool = False
    id: int | None = None


@dataclass
class ScriptCacheEntry:
    """脚本包本地缓存引用。"""

    script_id: str
    version: int
    sha256: str
    path: str


class Ledger(Protocol):
    """Agent 本地账本端口（鸭子类型）。"""

    def claim_run(self, run_id: str, attempt_no: int, device_ids: list[str] | None = None) -> bool:
        """原子 claim：首次（或新 attempt）返回 True；重复派发返回 False。

        ``device_ids`` 记录本次 Run 占用的物理设备集合（随 run.assign 下发），
        供心跳汇总占用状态上报 Master。
        """
        ...

    def get_run(self, run_id: str) -> AgentRun | None:
        """读取本地 Run 状态。"""
        ...

    def update_run(self, run: AgentRun) -> None:
        """更新 Run 状态/取消标志/结果摘要。"""
        ...

    def list_active_runs(self) -> list[AgentRun]:
        """返回未终结的 Run（claimed/running，用于心跳负载与恢复现场）。"""
        ...

    def record_inbox(self, origin_id: str, message_id: str, message_type: str) -> bool:
        """入站去重：已存在返回 False，首次记录返回 True。"""
        ...

    def enqueue_outbox(self, outbox_id: str, topic: str, payload: dict) -> None:
        """写入一条待发送出站消息。"""
        ...

    def replace_outbox(self, outbox_id: str, topic: str, payload: dict) -> None:
        """替换一条可重放消息（注册重连时使用同一逻辑消息 ID）。"""
        ...

    def claim_due_outbox(self, limit: int, now: datetime) -> list[AgentOutboxEntry]:
        """取到期待发送消息（next_attempt_at <= now）。"""
        ...

    def mark_outbox(
        self,
        outbox_id: str,
        *,
        status: AgentOutboxStatus,
        attempts: int,
        next_attempt_at: datetime | None,
        claimed_until: datetime | None = None,
    ) -> None:
        """更新出站消息发送状态与下次重试时间。"""
        ...

    def append_task_log(self, entry: TaskLogSpoolEntry) -> None:
        """追加一条任务日志到 spool。"""
        ...

    def list_pending_task_logs(self, limit: int) -> list[TaskLogSpoolEntry]:
        """取未上报的任务日志（按 sequence）。"""
        ...

    def mark_task_logs_published(self, ids: list[int]) -> None:
        """标记任务日志已上报。"""
        ...

    def get_published_log_stats(self, run_id: str) -> dict:
        """返回 run 已发布日志统计：{last_sequence, entry_count}。

        用于 run.log-complete 围栏声明；无日志时 last_sequence=0。
        """
        ...

    def cache_script(self, entry: ScriptCacheEntry) -> bool:
        """写入脚本缓存引用；重复返回 False。"""
        ...

    def get_cached_script(self, script_id: str, version: int, sha256: str) -> ScriptCacheEntry | None:
        """按 (script_id, version, sha256) 查缓存。"""
        ...
