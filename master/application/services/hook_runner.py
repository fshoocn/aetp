"""生命周期 Hook 框架（P8.4，§10.4/§10.6）。

两类 Hook 不可混用：
- AdmissionHook（准入）：事务提交前评估，默认 fail closed
- EventHook（事件）：领域事件持久化后异步消费，默认 fail open

Hook 按 (order, name) 稳定排序，执行结果写 hook_executions 审计。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from master.domain.hooks import (
    AdmissionHook,
    EventHook,
    HookContext,
    HookDecision,
)
from master.domain.models import DomainEvent
from master.domain.models.hook_execution import HookExecution
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0


@dataclass
class HookRegistry:
    """Hook 注册表（bootstrap 容器装配）。"""

    admission_hooks: list[AdmissionHook] = field(default_factory=list)
    event_hooks: list[EventHook] = field(default_factory=list)

    def sorted_admission(self, stage: str) -> list[AdmissionHook]:
        return sorted(
            [h for h in self.admission_hooks if h.stage == stage],
            key=lambda h: (h.order, h.name),
        )

    def matching_event_hooks(self, event_type: str) -> list[EventHook]:
        return [
            h for h in self.event_hooks
            if not h.event_types or event_type in h.event_types
        ]


class HookRunner:
    """Hook 执行器：排序、超时、审计写入。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        registry: HookRegistry | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry or HookRegistry()
        self._timeout_s = timeout_s

    @property
    def registry(self) -> HookRegistry:
        return self._registry

    def run_admission(
        self,
        stage: str,
        context: HookContext,
        *,
        timeout_s: float | None = None,
    ) -> HookDecision:
        """执行准入 Hook（fail closed）。

        超时、异常或 deny 均拒绝该命令。
        """
        hooks = self._registry.sorted_admission(stage)
        if not hooks:
            return HookDecision(allowed=True)

        timeout = timeout_s or self._timeout_s
        for hook in hooks:
            decision = self._run_single_admission(hook, context, timeout)
            if not decision.allowed and not decision.advisory:
                self._audit(hook.name, stage, "denied", context.project_id, error=decision.reason)
                return decision
            self._audit(hook.name, stage, "passed", context.project_id)

        return HookDecision(allowed=True)

    def run_event_hooks(self, event: DomainEvent) -> None:
        """执行事件 Hook（fail open）。"""
        hooks = self._registry.matching_event_hooks(event.event_type)
        for hook in hooks:
            self._run_single_event(hook, event)

    def _run_single_admission(
        self,
        hook: AdmissionHook,
        context: HookContext,
        timeout_s: float,
    ) -> HookDecision:
        try:
            decision = asyncio.run(
                asyncio.wait_for(hook.evaluate(context), timeout=timeout_s)
            )
            return decision
        except asyncio.TimeoutError:
            logger.warning("准入 Hook 超时: hook=%s stage=%s", hook.name, hook.stage)
            return HookDecision(allowed=False, reason=f"Hook {hook.name} 超时", code="HOOK_TIMEOUT")
        except Exception as exc:
            logger.exception("准入 Hook 异常: hook=%s stage=%s", hook.name, hook.stage)
            return HookDecision(
                allowed=False,
                reason=f"Hook {hook.name} 异常: {exc}",
                code="HOOK_EXECUTION_FAILED",
            )

    def _run_single_event(self, hook: EventHook, event: DomainEvent) -> None:
        start = time.monotonic()
        try:
            asyncio.run(
                asyncio.wait_for(hook.handle(event), timeout=self._timeout_s)
            )
            duration_ms = (time.monotonic() - start) * 1000
            self._audit(
                hook.name,
                event.event_type,
                "succeeded",
                event.project_id,
                event_id=event.event_id,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            logger.exception("事件 Hook 异常: hook=%s event=%s", hook.name, event.event_type)
            self._audit(
                hook.name,
                event.event_type,
                "failed",
                event.project_id,
                event_id=event.event_id,
                error=str(exc),
            )

    def _audit(
        self,
        hook_name: str,
        stage: str,
        status: str,
        project_id: str | None = None,
        *,
        event_id: str | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        with self._uow_factory() as uow:
            uow.hook_executions.add(
                HookExecution(
                    execution_id=f"HE-{uuid.uuid4().hex.upper()}",
                    event_id=event_id,
                    project_id=project_id,
                    hook_name=hook_name,
                    stage=stage,
                    status=status,
                    duration_ms=duration_ms,
                    error_message=error,
                    occurred_at=utcnow(),
                )
            )

    def list_executions(
        self,
        project_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[HookExecution]:
        with self._uow_factory() as uow:
            return uow.hook_executions.list_by_project(
                project_id, limit=limit, offset=offset
            )
