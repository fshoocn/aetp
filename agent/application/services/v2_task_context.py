"""V2 executor 的 Agent 运行上下文。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from aetp_protocol.execution import CaseStatus, ExecutionPlan
from aetp_protocol.ids import SessionId
from aetp_protocol.logs import LogLevel
from aetp_protocol.payloads import (
    CaseStatusEvent,
    ExecutionLogBatch,
    ExecutionLogEntry,
    ExecutionProgress,
    LogComplete,
)

from agent.application.services.execution_service import ExecutionCancelled
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.config import AgentSettings
from agent.domain.ledger import Ledger


class V2TaskContext:
    """只通过 V2 typed payload 向 Master 上报执行事实。"""

    def __init__(
        self,
        settings: AgentSettings,
        ledger: Ledger,
        publisher: AgentV2CapabilityPublisher,
        plan: ExecutionPlan,
        session_id: Callable[[], SessionId],
        is_cancelled: Callable[[], bool],
        *,
        script_ref: Mapping[str, Any] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._publisher = publisher
        self._plan = plan
        self._session_id = session_id
        self._is_cancelled = is_cancelled
        self._now = now or (lambda: datetime.now(UTC))
        self._progress_sequence = 0
        self._log_sequence = 0
        self._log_entry_count = 0
        self._case_sequences: dict[str, int] = {}
        self.task_id = plan.task_id.root
        self.shard_id = plan.shard_id.root
        self.run_id = plan.run_id.root
        self.project_id = plan.project_id.root
        self.node_id = plan.node_id.root
        self.params: Mapping[str, Any] = {
            **plan.configuration.values,
            **plan.execution_parameters,
        }
        self.script_ref: Mapping[str, Any] = dict(script_ref or plan.script.model_dump(mode="json"))
        self.case_keys = list(plan.case_keys)
        self.resources = plan.resource_bindings

    def is_cancelled(self) -> bool:
        return self._is_cancelled()

    async def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise ExecutionCancelled(f"run 已取消: {self.run_id}")

    async def progress(self, percent: int, stage: str, message: str = "") -> None:
        self._progress_sequence += 1
        run = self._ledger.get_run(self._plan.run_id.root)
        if run is not None:
            run.last_progress_sequence = max(run.last_progress_sequence, self._progress_sequence)
            self._ledger.update_run(run)
        self._publisher.enqueue_execution_progress(
            self._ledger,
            ExecutionProgress(
                run_id=self._plan.run_id,
                shard_id=self._plan.shard_id,
                attempt_id=self._plan.attempt_id,
                plan_id=self._plan.plan_id,
                sequence=self._progress_sequence,
                percent=percent,
                stage=stage,
                message=message,
            ),
            self._session_id(),
        )

    async def log(
        self,
        level: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        if not message:
            return
        normalized = {"warning": "warn", "critical": "error", "fatal": "error"}.get(
            level.strip().lower(),
            level.strip().lower(),
        )
        log_level = LogLevel(normalized)
        self._log_sequence += 1
        self._log_entry_count += 1
        self._publisher.enqueue_execution_log(
            self._ledger,
            ExecutionLogBatch(
                run_id=self._plan.run_id,
                shard_id=self._plan.shard_id,
                attempt_id=self._plan.attempt_id,
                plan_id=self._plan.plan_id,
                first_sequence=self._log_sequence,
                entries=(
                    ExecutionLogEntry(
                        sequence=self._log_sequence,
                        level=log_level,
                        message=message,
                        detail=dict(detail or {}),
                        occurred_at=self._now(),
                    ),
                ),
            ),
            self._session_id(),
        )

    async def capture_log(
        self,
        stream: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        merged = dict(detail or {})
        merged.setdefault("stream", stream)
        await self.log("info", message, merged)

    async def case_status(self, case_key: str, status: str) -> None:
        if case_key not in self.case_keys:
            raise ValueError(f"case_key 不属于当前 Plan: {case_key}")
        sequence = self._case_sequences.get(case_key, 0) + 1
        self._case_sequences[case_key] = sequence
        self._publisher.enqueue_execution_case_status(
            self._ledger,
            CaseStatusEvent(
                run_id=self._plan.run_id,
                shard_id=self._plan.shard_id,
                attempt_id=self._plan.attempt_id,
                plan_id=self._plan.plan_id,
                case_key=case_key,
                sequence=sequence,
                status=CaseStatus(status),
            ),
            self._session_id(),
        )

    async def complete_logs(self) -> None:
        self._publisher.enqueue_execution_log_complete(
            self._ledger,
            LogComplete(
                run_id=self._plan.run_id,
                shard_id=self._plan.shard_id,
                attempt_id=self._plan.attempt_id,
                plan_id=self._plan.plan_id,
                last_sequence=self._log_sequence,
                entry_count=self._log_entry_count,
            ),
            self._session_id(),
        )


__all__ = ["V2TaskContext"]
