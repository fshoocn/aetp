"""Agent 任务执行上下文（P6.2，§9.4/§9.5）。

``TaskContext`` 实现共享插件包 ``AgentTaskContext`` 协议，是插件在台架上
执行时的唯一上报入口：

- ``log`` / ``capture_log``：追加任务日志到本地 spool（``(run_id, sequence)``
  幂等，由账本 ``agent_task_log_spool`` 唯一约束保证）；
- ``progress``：进度上报（``run.progress``，sequence 单调递增，QoS0 可丢）；
- ``case_status``：case 级状态上报（``run.case-status``，仅支持实时 case 结果
  的插件，如 pytest；CANoe 类不发）；
- ``is_cancelled`` / ``raise_if_cancelled``：对接执行器的 ``CancellationToken``；
- ``build_log_batch``：从待上报 spool 生成 ``RunLogBatch``（严格按 sequence
  递增，``first_sequence`` 等于首条）。

插件只能通过本上下文上报进度与日志，不得直接依赖 MQTT/数据库（§9.5 规则 2）。
本模块只依赖 Ledger 端口与协议 DTO。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.logs import LogLevel, RunLogBatch, RunLogEntry
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    RunCaseStatusPayload,
    RunProgressPayload,
)
from aetp_protocol.topics import event_topic

from agent.config import AgentSettings
from agent.domain.ledger import Ledger, TaskLogSpoolEntry

logger = logging.getLogger(__name__)

# 日志等级归一化：接受任意大小写/别名，统一为小写 LogLevel 值。
_LEVEL_ALIASES = {
    "debug": "debug",
    "info": "info",
    "warn": "warn",
    "warning": "warn",
    "error": "error",
    "critical": "error",
    "fatal": "error",
}


def _normalize_level(level: str) -> str:
    normalized = _LEVEL_ALIASES.get(level.strip().lower(), level.strip().lower())
    if normalized not in LogLevel:
        raise ValueError(f"非法日志等级: {level!r}")
    return normalized


class TaskContext:
    """插件执行上下文：日志/进度/case 状态上报 + 取消信号。"""

    def __init__(
        self,
        settings: AgentSettings,
        ledger: Ledger,
        *,
        project_id: str,
        task_id: str,
        shard_id: str,
        run_id: str,
        node_id: str | None = None,
        params: Mapping[str, Any] | None = None,
        script_ref: Mapping[str, Any] | None = None,
        case_keys: list[str] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        session_id: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self.task_id = task_id
        self.shard_id = shard_id
        self.run_id = run_id
        self.project_id = project_id
        self.node_id = node_id or settings.node_id
        self.params: Mapping[str, Any] = dict(params or {})
        self.script_ref: Mapping[str, Any] = dict(script_ref or {})
        self.case_keys = list(case_keys or [])
        self._is_cancelled = is_cancelled or (lambda: False)
        self._session_id = session_id or (lambda: self._settings.node_id)
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Run 内日志序号：从 1 递增；账本 (run_id, sequence) 唯一约束保证幂等。
        self._sequence = 0

    # -- 取消 ---------------------------------------------------------------

    def is_cancelled(self) -> bool:
        """当前 Run 是否已被请求取消（§9.5 规则 6）。"""
        return self._is_cancelled()

    async def raise_if_cancelled(self) -> None:
        """已取消则抛 ``ExecutionCancelled``，供插件在长循环/等待后检查。"""
        if self.is_cancelled():
            # 延迟导入避免与 execution_service 的循环依赖
            from agent.application.services.execution_service import (
                ExecutionCancelled,
            )

            raise ExecutionCancelled(f"run 已取消: {self.run_id}")

    # -- 日志 ---------------------------------------------------------------

    async def log(
        self,
        level: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """追加一条任务日志到 spool（(run_id, sequence) 幂等）。"""
        if not message:
            return
        self._sequence += 1
        entry = TaskLogSpoolEntry(
            run_id=self.run_id,
            sequence=self._sequence,
            level=_normalize_level(level),
            message=message,
            detail=dict(detail or {}),
        )
        self._ledger.append_task_log(entry)

    async def capture_log(
        self,
        stream: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """采集外部进程日志流：以 ``detail.stream`` 标注来源后写 spool。"""
        merged = dict(detail or {})
        merged.setdefault("stream", stream)
        await self.log("info", message, merged)

    # -- 进度 ---------------------------------------------------------------

    async def progress(
        self, percent: int, stage: str, message: str = ""
    ) -> None:
        """进度上报：构造 run.progress 并写入 outbox（QoS0 语义，可覆盖）。"""
        self._sequence += 1
        payload = RunProgressPayload(
            run_id=self.run_id,
            sequence=self._sequence,
            percent=percent,
            stage=stage,
            message=message,
        )
        # 稳定 outbox ID：同 sequence 幂等，进度可被后续同 sequence 覆盖
        outbox_id = f"progress:{self.run_id}:{self._sequence}"
        self._enqueue_event(
            MessageType.RUN_PROGRESS,
            "progress",
            outbox_id,
            payload.model_dump(mode="json"),
        )

    # -- case 状态 ----------------------------------------------------------

    async def case_status(self, case_key: str, status: str) -> None:
        """case 级状态上报（仅支持实时 case 结果的插件，§8.4）。"""
        payload = RunCaseStatusPayload(
            run_id=self.run_id,
            case_key=case_key,
            status=status,
        )
        # 稳定 outbox ID：同一 case 幂等，后到状态覆盖前值
        outbox_id = f"case-status:{self.run_id}:{case_key}"
        self._enqueue_event(
            MessageType.RUN_CASE_STATUS,
            "case-status",
            outbox_id,
            payload.model_dump(mode="json"),
        )

    # -- 日志批生成 ---------------------------------------------------------

    def collect_pending_logs(self, limit: int = 50) -> list[TaskLogSpoolEntry]:
        """取本 Run 未上报的日志条目（按 sequence 升序）。"""
        return [
            entry
            for entry in self._ledger.list_pending_task_logs(limit)
            if entry.run_id == self.run_id
        ]

    def build_log_batch(
        self, entries: list[TaskLogSpoolEntry]
    ) -> RunLogBatch | None:
        """把 spool 条目组装为 ``RunLogBatch``（严格递增，first_sequence=首条）。

        条目不足 1 条返回 None。``RunLogEntry`` 的 project/task/node 取自本
        上下文元数据，occurred_at 用当前时间（spool 未持久化 occurred_at）。
        """
        if not entries:
            return None
        occurred_at = self._now()
        run_entries = [
            RunLogEntry(
                project_id=self.project_id,
                task_id=self.task_id,
                run_id=self.run_id,
                node_id=self.node_id,
                sequence=entry.sequence,
                level=LogLevel(entry.level),
                message=entry.message,
                detail=dict(entry.detail or {}),
                occurred_at=occurred_at,
            )
            for entry in entries
        ]
        return RunLogBatch(
            run_id=self.run_id,
            first_sequence=run_entries[0].sequence,
            entries=run_entries,
        )

    def mark_logs_published(self, ids: list[int]) -> None:
        """标记日志已上报（outbox 发送成功后调用）。"""
        self._ledger.mark_task_logs_published(ids)

    # -- 内部 ---------------------------------------------------------------

    def _enqueue_event(
        self,
        message_type: MessageType,
        segment: str,
        outbox_id: str,
        payload: dict,
    ) -> None:
        envelope = Envelope(
            message_id=uuid.uuid4().hex,
            message_type=message_type.value,
            sent_at=self._now(),
            sender=Sender(
                kind=SenderKind.AGENT,
                id=self._settings.node_id,
                session_id=self._session_id(),
            ),
            trace_id=self.run_id,
            payload=payload,
        )
        topic = event_topic(self._settings.node_id, segment)
        self._ledger.replace_outbox(
            outbox_id,
            topic,
            envelope.model_dump(mode="json"),
        )
