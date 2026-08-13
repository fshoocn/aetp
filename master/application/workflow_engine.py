"""执行编排层：工作流执行引擎（WorkflowEngine）。

统一推进 WorkflowSpec 阶段：
- 按 stage 定义经 WorkflowActionRunner 端口执行动作（调插件/MQTT/落库）
- 处理重试（stage.retry）与超时（stage.timeout_s）
- 阶段推进产生领域事件（stage_entered/stage_succeeded/stage_failed/
  workflow.succeeded/workflow.failed），经 EventStore 持久化（可观测/回放）

引擎只依赖端口（WorkflowActionRunner + EventStore），不依赖 adapter。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Protocol

from master.domain.models import DomainEvent
from master.domain.time import utcnow
from master.domain.workflow import WorkflowProgress, WorkflowSpec

logger = logging.getLogger(__name__)


class WorkflowActionRunner(Protocol):
    """工作流阶段动作执行器端口（业务层注入实现，只依赖端口）。"""

    async def run(self, action: str, context: Mapping[str, Any]) -> bool:
        """执行一个阶段动作；返回是否成功（True=继续下一阶段）。"""
        ...


class WorkflowEngine:
    """统一工作流执行引擎。

    用法（由应用服务调用）：
        progress = WorkflowProgress(aggregate_id=..., stage=spec.start, context=...)
        await engine.advance(spec, progress, runner)
        # 调用方持久化 progress.stage / attempts / error
    """

    def __init__(self, event_store: Any) -> None:
        # event_store: EventStore 端口（P3.7），阶段事件经此持久化
        self._event_store = event_store

    async def advance(
        self,
        spec: WorkflowSpec,
        progress: WorkflowProgress,
        runner: WorkflowActionRunner,
    ) -> WorkflowProgress:
        """从 progress 当前阶段推进到终态（或失败）。返回更新后的 progress。"""
        while not progress.is_terminal(spec):
            stage = spec.stages[progress.stage]
            progress.attempts = 0
            progress.error = None
            self._emit(spec, progress, f"{spec.aggregate_type}.stage_entered")

            ok = False
            for _ in range(stage.retry + 1):
                progress.attempts += 1
                ok = await self._run_action(runner, stage, progress)
                if ok:
                    break

            if ok:
                self._emit(spec, progress, f"{spec.aggregate_type}.stage_succeeded")
                progress.stage = spec.next_stage(progress.stage, ok)
            else:
                progress.error = progress.error or f"阶段 {stage.name} 失败"
                self._emit(spec, progress, f"{spec.aggregate_type}.stage_failed")
                progress.stage = spec.next_stage(progress.stage, ok)

        if progress.stage == spec.terminal_success:
            self._emit(spec, progress, f"{spec.aggregate_type}.workflow.succeeded")
        else:
            self._emit(spec, progress, f"{spec.aggregate_type}.workflow.failed")
        return progress

    async def _run_action(
        self,
        runner: WorkflowActionRunner,
        stage: Any,
        progress: WorkflowProgress,
    ) -> bool:
        """执行阶段动作，捕获超时/异常为失败并记录到 progress.error。"""
        try:
            if stage.timeout_s > 0:
                return await asyncio.wait_for(
                    runner.run(stage.action, progress.context),
                    timeout=stage.timeout_s,
                )
            return await runner.run(stage.action, progress.context)
        except asyncio.TimeoutError:
            progress.error = f"阶段 {stage.name} 超时（>{stage.timeout_s}s）"
            logger.warning("workflow stage timeout: %s/%s", progress.aggregate_id, stage.name)
            return False
        except Exception as exc:  # noqa: BLE001 - 动作异常统一按阶段失败处理
            progress.error = f"阶段 {stage.name} 异常: {exc}"
            logger.exception("workflow stage error: %s/%s", progress.aggregate_id, stage.name)
            return False

    def _emit(self, spec: WorkflowSpec, progress: WorkflowProgress, event_type: str) -> None:
        """阶段事件持久化（不可变领域事件，供 SSE/Hook/审计消费）。"""
        try:
            self._event_store.append(
                DomainEvent(
                    event_id="",  # 由 EventStore 实现分配（此处仅占位）
                    project_id=progress.context.get("project_id"),
                    event_type=event_type,
                    aggregate_id=progress.aggregate_id,
                    payload={
                        "stage": progress.stage,
                        "attempts": progress.attempts,
                        "error": progress.error,
                    },
                    occurred_at=utcnow(),
                )
            )
        except Exception:  # noqa: BLE001 - 事件持久化失败不阻塞工作流推进（fail open）
            logger.warning("workflow event persist failed: %s", event_type)
