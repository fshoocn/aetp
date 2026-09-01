"""Agent V2 ExecutionPlan 执行桥接。"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aetp_protocol.errors import ErrorCode
from aetp_protocol.execution import (
    CaseResult,
    ExecutionPlan,
    ExecutionStatus,
)
from aetp_protocol.execution import (
    ExecutionError as ProtocolExecutionError,
)
from aetp_protocol.execution import (
    ExecutionResult as ProtocolExecutionResult,
)
from aetp_protocol.ids import MessageId, SessionId
from aetp_protocol.payloads import ExecutionFinished

from agent.application.services.execution_service import (
    ExecutionResult as AgentExecutionResult,
)
from agent.application.services.execution_service import (
    ExecutionService,
)
from agent.application.services.script_archive import extract_zip_safely
from agent.application.services.script_cache_service import ScriptCacheService
from agent.application.services.task_context import TaskContext
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.config import AgentSettings
from agent.domain.ledger import Ledger

logger = logging.getLogger(__name__)


class V2ExecutionRunner:
    """把已预检的 V2 Plan 交给精确 executor 并发布 finished 事实。"""

    def __init__(
        self,
        settings: AgentSettings,
        ledger: Ledger,
        execution_service: ExecutionService,
        publisher: AgentV2CapabilityPublisher,
        executor_resolver: Callable[[ExecutionPlan], Any],
        *,
        script_cache: ScriptCacheService | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._execution_service = execution_service
        self._publisher = publisher
        self._executor_resolver = executor_resolver
        self._script_cache = script_cache
        self._now = now or (lambda: datetime.now(UTC))
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._completed: set[str] = set()
        self._script_dirs: dict[str, Path] = {}

    def start(
        self,
        plan: ExecutionPlan,
        session_id: SessionId,
        *,
        correlation_id: MessageId | None = None,
    ) -> asyncio.Task[None] | None:
        """启动一次 Plan；同一 plan_id 重复消息不会重复执行。"""
        if plan.plan_id.root in self._completed:
            return None
        existing = self._tasks.get(plan.plan_id.root)
        if existing is not None:
            return existing
        task = asyncio.create_task(self._run(plan, session_id, correlation_id))
        self._tasks[plan.plan_id.root] = task
        task.add_done_callback(lambda _task: self._tasks.pop(plan.plan_id.root, None))
        return task

    async def stop(self) -> None:
        """取消并等待所有 V2 Plan 执行任务。"""
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(
        self,
        plan: ExecutionPlan,
        session_id: SessionId,
        correlation_id: MessageId | None,
    ) -> None:
        try:
            plugin = self._executor_resolver(plan)
            script_ref = plan.script.model_dump(mode="json")
            if self._script_cache is not None:
                cached = await asyncio.to_thread(self._script_cache.ensure_cached, script_ref)
                script_ref["path"] = await asyncio.to_thread(
                    self._prepare_script_directory,
                    cached.path,
                    plan.run_id.root,
                )
            context = TaskContext(
                self._settings,
                self._ledger,
                project_id=plan.project_id.root,
                task_id=plan.task_id.root,
                shard_id=plan.shard_id.root,
                run_id=plan.run_id.root,
                node_id=plan.node_id.root,
                params={**plan.configuration.values, **plan.execution_parameters},
                script_ref=script_ref,
                case_keys=list(plan.case_keys),
                is_cancelled=lambda: self._execution_service.is_cancelled(plan.run_id.root),
                session_id=lambda: session_id.root,
                now=self._now,
            )
            timeout_s = max(1, int((plan.deadline_at - self._now()).total_seconds()))
            result = await self._execution_service.execute(
                plan.run_id.root,
                plugin,
                context,
                timeout_s=timeout_s,
            )
            finished_result = await self._to_protocol_result(result, plugin, context)
        except Exception as exc:  # noqa: BLE001 - execution.finished 统一失败事实
            logger.exception("V2 Plan 执行桥接失败: plan=%s", plan.plan_id.root)
            finished_result = ProtocolExecutionResult(
                status=ExecutionStatus.FAILED,
                passed=False,
                error=ProtocolExecutionError(
                    code=ErrorCode("EXECUTION_FAILED"),
                    message=f"{type(exc).__name__}: {exc}",
                ),
            )
        finished = ExecutionFinished(
            run_id=plan.run_id,
            shard_id=plan.shard_id,
            attempt_id=plan.attempt_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            result=finished_result,
            finished_at=self._now(),
        )
        try:
            self._publisher.enqueue_execution_finished(
                self._ledger,
                finished,
                session_id,
                correlation_id=correlation_id,
            )
            self._completed.add(plan.plan_id.root)
        finally:
            self._cleanup_script_directory(plan.run_id.root)

    def _prepare_script_directory(self, source: str, run_id: str) -> str:
        source_path = Path(source)
        workspace = Path(self._settings.script_cache_dir).resolve().parent / "runs"
        workspace.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix="aetp-v2-run-", dir=str(workspace)))
        try:
            if zipfile.is_zipfile(source_path):
                extract_zip_safely(source_path, target)
            else:
                shutil.copy2(source_path, target / "test_script.py")
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        self._script_dirs[run_id] = target
        return str(target)

    def _cleanup_script_directory(self, run_id: str) -> None:
        directory = self._script_dirs.pop(run_id, None)
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    async def _to_protocol_result(
        self,
        result: AgentExecutionResult,
        plugin,
        context: TaskContext,
    ) -> ProtocolExecutionResult:
        status = {
            "succeeded": ExecutionStatus.SUCCEEDED,
            "failed": ExecutionStatus.FAILED,
            "cancelled": ExecutionStatus.CANCELLED,
            "timed_out": ExecutionStatus.TIMED_OUT,
        }[result.status.value]
        summary = dict(result.summary or {})
        if status is ExecutionStatus.SUCCEEDED and hasattr(plugin, "analyze_results"):
            try:
                analysis = await plugin.analyze_results(summary, context)
            except Exception as exc:  # noqa: BLE001 - 分析异常转统一失败结果
                return ProtocolExecutionResult(
                    status=ExecutionStatus.FAILED,
                    passed=False,
                    error=ProtocolExecutionError(
                        code=ErrorCode("EXECUTION_FAILED"),
                        message=f"结果分析失败: {type(exc).__name__}: {exc}",
                    ),
                )
            if isinstance(analysis, Mapping):
                summary = dict(analysis)
                if analysis.get("passed") is False:
                    status = ExecutionStatus.FAILED
        case_results = tuple(
            CaseResult.model_validate(item)
            for item in summary.get("case_results", ())
            if isinstance(item, Mapping)
        )
        error = None
        if status is not ExecutionStatus.SUCCEEDED:
            code = {
                ExecutionStatus.CANCELLED: "EXECUTION_CANCELLED",
                ExecutionStatus.TIMED_OUT: "EXECUTION_TIMED_OUT",
            }.get(status, "EXECUTION_FAILED")
            error = ProtocolExecutionError(
                code=ErrorCode(code),
                message=result.error or str(summary.get("error") or "V2 executor failed"),
            )
        return ProtocolExecutionResult(
            status=status,
            passed=status is ExecutionStatus.SUCCEEDED and summary.get("passed", True) is not False,
            case_results=case_results,
            metrics=dict(summary.get("metrics") or {}),
            data=dict(summary.get("data") or {}),
            error=error,
        )


__all__ = ["V2ExecutionRunner"]
