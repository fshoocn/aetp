"""HookChain 宿主：test_core 的三条数据改写链（DESIGN §11.2）。

数据经 :class:`HookCtx`（可变 dataclass）传递：``task.transform`` 可改
``spec``、``result.transform`` 可改 ``result``、``artifact.sink`` 只读 ``ref``。
拦截器经 :meth:`TestHooks.register` 登记，``phase + order`` 定序、``on_error``
逐条声明失败策略；修改数据只能走 HookChain，拦截器之间禁止直接互调。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from common.hook_chain import HookChain, Interceptor, OnError
from common.test_contracts import (
    CHAIN_ARTIFACT_SINK,
    CHAIN_RESULT_TRANSFORM,
    CHAIN_TASK_TRANSFORM,
    ArtifactRef,
    JobResult,
    TaskSpec,
)

#: 三条链共用的阶段词表（外 → 内）：前置观察 → 廉价校验 → 改写 → 最贴近数据
HOOK_PHASES: tuple[str, ...] = ("observe", "guard", "transform", "inner")


@dataclass(slots=True)
class HookCtx:
    """一次穿链执行的现场：宿主填上下文，拦截器按链别读写 spec / result / ref。"""

    #: 链名（CHAIN_* 常量之一），由宿主填
    chain: str
    #: 任务规格（task.transform 可改）
    spec: TaskSpec | None = None
    #: Job 结果（result.transform 可改）
    result: JobResult | None = None
    #: 产物引用（artifact.sink 只读）
    ref: ArtifactRef | None = None
    #: 宿主上下文（task_id / job_id 等），拦截器可自由读写
    meta: dict[str, Any] = field(default_factory=dict)


def _owner_of(fn: object) -> str:
    """拦截器归属标签：取定义模块的顶层包名（排序 / 统计用）。"""
    module = getattr(fn, "__module__", "") or ""
    return module.split(".", 1)[0] or "unknown"


class TestHooks:
    """三条 HookChain 的宿主：登记拦截器、穿链执行、汇总自省统计（§11.2）。"""

    #: 三条链的链名（登记与自省的合法取值）
    chains: tuple[str, ...] = (CHAIN_TASK_TRANSFORM, CHAIN_RESULT_TRANSFORM, CHAIN_ARTIFACT_SINK)

    def __init__(self, logger: Any | None = None) -> None:
        self._logger: Any = logger if logger is not None else logging.getLogger("test_core.hooks")
        self._chains: dict[str, HookChain[HookCtx, HookCtx]] = {
            chain: HookChain(HOOK_PHASES, on_problem=self._report) for chain in self.chains
        }

    def _report(self, message: str) -> None:
        """``SKIP`` 出错的告警出口（记 warning，不静默）。"""
        self._logger.warning(message)

    def register(
        self,
        chain: str,
        fn: Interceptor[HookCtx, HookCtx],
        *,
        phase: str,
        order: int,
        name: str,
        on_error: OnError = OnError.FAIL,
    ) -> Callable[[], None]:
        """登记拦截器到指定链（§11.2），返回注销函数（绑调用方生命周期）。

        ``phase/order`` 定序（词表见 :data:`HOOK_PHASES`）；归属 ``owner`` 取
        拦截器定义模块根。非法链名抛 ``ValueError``。
        """
        if chain not in self._chains:
            raise ValueError(f"未知链 {chain!r}，可选：{list(self.chains)}")
        return self._chains[chain].use(
            fn, phase=phase, owner=_owner_of(fn), name=name, order=order, on_error=on_error
        )

    async def run_task_transform(self, ctx: HookCtx) -> HookCtx:
        """穿过 task.transform 链（可改 ``ctx.spec``），terminal 原样返回 ctx。"""
        return await self._run(CHAIN_TASK_TRANSFORM, ctx)

    async def run_result_transform(self, ctx: HookCtx) -> HookCtx:
        """穿过 result.transform 链（可改 ``ctx.result``），terminal 原样返回 ctx。"""
        return await self._run(CHAIN_RESULT_TRANSFORM, ctx)

    async def run_artifact_sink(self, ctx: HookCtx) -> HookCtx:
        """穿过 artifact.sink 链（``ctx.ref`` 只读），terminal 原样返回 ctx。"""
        return await self._run(CHAIN_ARTIFACT_SINK, ctx)

    async def _run(self, chain: str, ctx: HookCtx) -> HookCtx:
        """穿指定链执行；terminal 保持 ctx 原样（数据改写全由拦截器完成）。"""

        async def terminal(inner: HookCtx) -> HookCtx:
            return inner

        return await self._chains[chain].run(ctx, terminal)

    def stats(self) -> dict[str, object]:
        """三条链的 HookChain 自省统计（调用 / 出错 / 自身耗时，挂 /healthz 用）。"""
        return {chain: self._chains[chain].stats() for chain in self.chains}


__all__: list[str] = ["HOOK_PHASES", "HookCtx", "TestHooks"]
