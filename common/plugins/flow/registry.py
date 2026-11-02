"""步骤 / 模板注册表（Phase 2；§5.3 注册语义、§5.12 排空）。

* 步骤注册绑定调用方 fiber（传入的 ``fiber``），卸载自动摘除；
* ``(stage, step_id)`` 节点内唯一：同 provider 重复注册（如重载）→ 替换，
  不同 provider 抢同 id → ``ValueError``；
* 注册项信封化（``common.capabilities``，``kind = "flow.step"``），
  步骤目录 = 信封列表；
* :meth:`StepHandle.drain` 实现 §5.12 的排空：置「排空中」→ 等在途尝试
  结束（超时则置 ``sc.cancelled``，由执行器的收尾 / 强制取消逻辑接管）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from cordis_port import Fiber, FiberState

from common.capabilities import Capability
from common.flow_contracts import (
    STAGE_LABEL,
    STEP_KIND,
    FlowDef,
    JsonValue,
    StepContext,
    StepHandler,
)


class Attempt:
    """一次在途尝试的登记（排空与取消的目标）。"""

    __slots__ = ("sc", "task")

    def __init__(self, sc: StepContext) -> None:
        self.sc: StepContext = sc
        self.task: asyncio.Task[object] | None = None


class StepRecord:
    """一条步骤注册（提供者 + 执行策略 + 在途状态）。"""

    __slots__ = (
        "_attempts",
        "_idle",
        "_semaphore",
        "closed",
        "drained",
        "fiber",
        "handler",
        "idempotent",
        "labels",
        "max_concurrency",
        "params_schema",
        "priority",
        "provider",
        "stage",
        "step_id",
        "summary",
        "version",
    )

    def __init__(
        self,
        *,
        stage: str,
        step_id: str,
        handler: StepHandler,
        labels: Mapping[str, str],
        priority: int,
        version: str,
        summary: str,
        params_schema: Mapping[str, JsonValue] | None,
        max_concurrency: int,
        idempotent: bool,
        provider: str,
        fiber: Fiber,
    ) -> None:
        self.stage = stage
        self.step_id = step_id
        self.handler = handler
        self.labels = dict(labels)
        self.priority = priority
        self.version = version
        self.summary = summary
        self.params_schema = params_schema
        self.max_concurrency = max_concurrency
        self.idempotent = idempotent
        self.provider = provider
        self.fiber = fiber
        self.closed = False
        self.drained = False
        self._attempts: set[Attempt] = set()
        self._semaphore = (
            asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        )
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def alive(self) -> bool:
        """提供者 fiber 仍 ACTIVE 且未排空（§5.6 的 ``alive`` 判定）。"""
        return (
            not self.closed
            and not self.drained
            and self.fiber.uid is not None
            and self.fiber.state is FiberState.ACTIVE
        )

    def selector_labels(self) -> dict[str, str]:
        """信封标签（阶段放 ``labels["stage"]``，选择器子集匹配的轴）。"""
        return {STAGE_LABEL: self.stage, **self.labels}

    def envelope(self) -> Capability:
        """能力信封（``flow.step``；``detail`` 带参数 schema 与执行策略）。"""
        return Capability(
            kind=STEP_KIND,
            id=f"{self.stage}/{self.step_id}",
            provider=self.provider,
            version=self.version,
            labels=self.selector_labels(),
            summary=self.summary,
            detail={
                "params_schema": dict(self.params_schema) if self.params_schema else None,
                "idempotent": self.idempotent,
                "max_concurrency": self.max_concurrency,
            },
        )

    def begin(self, sc: StepContext) -> Attempt:
        """登记一次在途尝试（排空据此等待 / 取消）。"""
        attempt = Attempt(sc)
        self._attempts.add(attempt)
        self._idle.clear()
        return attempt

    def end(self, attempt: Attempt) -> None:
        self._attempts.discard(attempt)
        if not self._attempts:
            self._idle.set()

    async def acquire_slot(self) -> None:
        """占用一个并发槽（``max_concurrency`` > 0 时阻塞等待；0 = 不限）。"""
        if self._semaphore is not None:
            await self._semaphore.acquire()

    def release_slot(self) -> None:
        if self._semaphore is not None:
            self._semaphore.release()

    async def wait_idle(self, timeout: float | None) -> bool:
        """等在途尝试清零；超时返回 ``False``。"""
        if not self._attempts:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            return False
        return True

    @property
    def attempts(self) -> frozenset[Attempt]:
        return frozenset(self._attempts)


class StepHandle:
    """``ctx.flow.step`` 返回的注册句柄（契约 :class:`StepRegistration`）。"""

    def __init__(
        self, registry: "StepRegistry", record: StepRecord, cancel_grace: float
    ) -> None:
        self._registry = registry
        self._record = record
        self._cancel_grace = cancel_grace

    @property
    def alive(self) -> bool:
        return self._record.alive

    async def drain(self, timeout: float | None = None) -> None:
        """停接新活 → 等在途尝试结束 → 超时则置取消信号（幂等，§5.12）。

        强制收尾由执行器统一做（置 ``sc.cancelled`` 后 ``cancel_grace`` 内
        协作退出，仍不退出才 ``task.cancel()``），这里只发信号 + 等待。
        """
        record = self._record
        record.drained = True
        if await record.wait_idle(timeout):
            return
        for attempt in record.attempts:
            attempt.sc.cancelled.set()
        await record.wait_idle(self._cancel_grace)
        for attempt in record.attempts:
            if attempt.task is not None:
                attempt.task.cancel()

    def close(self) -> None:
        """显式注销（幂等；fiber 卸载会经 effect 自动走同一条路）。"""
        self._registry.remove(self._record)


class StepRegistry:
    """步骤与模板的登记簿（内存态，Phase 2 重启即丢）。"""

    def __init__(
        self, *, cancel_grace: float, on_change: Callable[[str, str, str, str], None]
    ) -> None:
        self._records: dict[tuple[str, str], StepRecord] = {}
        self._flows: dict[str, tuple[FlowDef, Fiber]] = {}
        self._cancel_grace = cancel_grace
        #: 注册变更回调 ``(stage, step_id, action, provider)``——service 派发事件
        self._on_change = on_change

    # -- 步骤 ----------------------------------------------------------------
    def register_step(
        self,
        *,
        stage: str,
        step_id: str,
        handler: StepHandler,
        labels: Mapping[str, str],
        priority: int,
        version: str,
        summary: str,
        params_schema: Mapping[str, JsonValue] | None,
        max_concurrency: int,
        idempotent: bool,
        provider: str,
        fiber: Fiber,
    ) -> StepHandle:
        key = (stage, step_id)
        existing = self._records.get(key)
        if existing is not None and existing.provider != provider:
            raise ValueError(
                f"步骤 {stage}/{step_id} 已由提供者 {existing.provider!r} 注册"
                f"（{provider!r} 抢注同 id）；需要并存用不同 step_id + labels 区分"
            )
        if existing is not None:
            self.remove(existing, notify=False)
        record = StepRecord(
            stage=stage,
            step_id=step_id,
            handler=handler,
            labels=labels,
            priority=priority,
            version=version,
            summary=summary,
            params_schema=params_schema,
            max_concurrency=max_concurrency,
            idempotent=idempotent,
            provider=provider,
            fiber=fiber,
        )
        self._records[key] = record
        self._on_change(stage, step_id, "added", provider)
        return StepHandle(self, record, self._cancel_grace)

    def remove(self, record: StepRecord, *, notify: bool = True) -> None:
        key = (record.stage, record.step_id)
        if self._records.get(key) is not record:
            return
        record.closed = True
        record.drained = True
        del self._records[key]
        if notify:
            self._on_change(record.stage, record.step_id, "removed", record.provider)

    def records(self) -> list[StepRecord]:
        """全部注册项（含已排空 / 已失效——``alive`` 由调用方判断）。"""
        return list(self._records.values())

    def alive_records(self) -> list[StepRecord]:
        return [record for record in self._records.values() if record.alive]

    # -- 模板 ----------------------------------------------------------------
    def register_flow(self, flow: FlowDef, fiber: Fiber) -> Callable[[], None]:
        """登记流程模板（绑 fiber）；同 id 冲突抛 ``ValueError``（§5.3）。"""
        if flow.id in self._flows:
            raise ValueError(f"流程模板已注册：{flow.id!r}")
        self._flows[flow.id] = (flow, fiber)

        def dispose() -> None:
            if self._flows.get(flow.id, (None, None))[0] is flow:
                del self._flows[flow.id]

        return dispose

    def flow(self, flow_id: str) -> FlowDef | None:
        entry = self._flows.get(flow_id)
        if entry is None:
            return None
        flow, fiber = entry
        if fiber.uid is None:
            return None
        return flow

    def flows(self) -> list[FlowDef]:
        """全部在册模板（fiber 仍有效的；用户流程列表合并用）。"""
        return [
            flow
            for flow, fiber in self._flows.values()
            if fiber.uid is not None
        ]


__all__: list[str] = ["Attempt", "StepHandle", "StepRecord", "StepRegistry"]
