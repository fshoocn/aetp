"""Agent 执行服务（P6.1，§9.6 阶段 E）。

``ExecutionService`` 是 Agent 执行已 claim Run 的唯一编排者，负责：

1. **并发上限**：以 ``max_concurrent_runs`` 为上限的 ``Semaphore`` 排队，
   超出上限的 Run 等待空闲槽位（默认单台架串行执行）；
2. **超时**：对插件 ``execute`` 施加 ``timeout_s`` 超时（0 = 不限制），
   超时映射为 ``TIMED_OUT``；
3. **取消 token**：每个 Run 一个 ``CancellationToken``，``cancel`` 触发后，
   排队中/执行中的 Run 在检查点抛 ``ExecutionCancelled``，映射为 ``CANCELLED``；
4. **异常映射**：插件执行异常统一映射为结构化 ``ExecutionResult``
   （SUCCEEDED / FAILED / CANCELLED / TIMED_OUT），并回写本地账本。

取消语义（§9.5 规则 6 / §9.8）：

- ``cancel`` 设置 token 并把账本 ``cancelled`` 置位；
- 排队中的 Run 在获得并发槽位后、执行前检查 token，立即以 ``CANCELLED`` 结束；
- 执行中的 Run 由 ``token.wait()`` 与插件任务竞争，token 先完成则取消插件
  任务并标记 ``CANCELLED``；插件自身也可经 ``context.raise_if_cancelled()``
  主动中止（P6.2 TaskContext 接入后生效）。

本模块只依赖 Ledger 端口与插件协议，不接触 MQTT/HTTP 框架。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus
from agent.domain.ledger import Ledger

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """执行错误基类（取消/超时等受控终止）。"""


class ExecutionCancelled(ExecutionError):
    """Run 被取消。"""


class ExecutionTimedOut(ExecutionError):
    """Run 执行超时。"""


class CancellationToken:
    """单个 Run 的取消信号（asyncio.Event 封装）。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        """置位取消信号（幂等）。"""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        """等待取消信号（已取消则立即返回）。"""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """已取消则抛 ExecutionCancelled。"""
        if self.cancelled:
            raise ExecutionCancelled("run 已取消")


@dataclass(frozen=True)
class ExecutionResult:
    """一次执行的最终结果（调用方据此上报 run.result）。"""

    run_id: str
    status: AgentRunStatus
    summary: dict = field(default_factory=dict)
    error: str = ""


class ExecutionService:
    """执行已 claim Run：并发上限 + 超时 + 取消 token + 异常映射。"""

    def __init__(self, settings: AgentSettings, ledger: Ledger) -> None:
        if settings.max_concurrent_runs <= 0:
            raise ValueError("max_concurrent_runs 必须大于 0")
        self._max_concurrent = settings.max_concurrent_runs
        self._ledger = ledger
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._tokens: dict[str, CancellationToken] = {}
        self._active: set[str] = set()

    # -- 状态查询 -----------------------------------------------------------

    @property
    def max_concurrent_runs(self) -> int:
        return self._max_concurrent

    @property
    def running(self) -> frozenset[str]:
        """当前正在执行（已获并发槽位）的 run_id 集合。"""
        return frozenset(self._active)

    def is_cancelled(self, run_id: str) -> bool:
        """run 是否已被请求取消。"""
        token = self._tokens.get(run_id)
        return token is not None and token.cancelled

    # -- 取消 ---------------------------------------------------------------

    def cancel(self, run_id: str) -> bool:
        """请求取消 run；返回是否命中执行器中的活跃 token。

        无论 token 是否存在，都同步把账本 ``cancelled`` 置位（run.cancel 语义），
        使尚未进入执行器的 Run 在后续执行前检查到取消。
        """
        token = self._tokens.get(run_id)
        if token is not None:
            token.cancel()
        run = self._ledger.get_run(run_id)
        if run is not None:
            run.cancelled = True
            self._ledger.update_run(run)
        return token is not None

    # -- 执行 ---------------------------------------------------------------

    async def execute(
        self,
        run_id: str,
        plugin,
        context,
        timeout_s: int = 0,
    ) -> ExecutionResult:
        """执行一个已 claim 的 Run，返回最终结果并回写账本。"""
        token = self._tokens.setdefault(run_id, CancellationToken())
        try:
            summary = await self._bounded_run(
                run_id, plugin, context, token, timeout_s
            )
            status = AgentRunStatus.SUCCEEDED
            error = ""
        except ExecutionCancelled as exc:
            status = AgentRunStatus.CANCELLED
            summary, error = {}, str(exc)
        except ExecutionTimedOut as exc:
            status = AgentRunStatus.TIMED_OUT
            summary, error = {}, str(exc)
        except Exception as exc:  # noqa: BLE001 - 插件异常统一映射 FAILED
            status = AgentRunStatus.FAILED
            summary, error = {}, f"{type(exc).__name__}: {exc}"
            logger.warning(
                "run 执行失败: run_id=%s error=%s", run_id, exc
            )
        finally:
            self._tokens.pop(run_id, None)
        return self._finalize(run_id, status, summary, error)

    async def _bounded_run(
        self,
        run_id: str,
        plugin,
        context,
        token: CancellationToken,
        timeout_s: int,
    ) -> dict:
        """在并发上限内执行，排队与执行前都检查取消。"""
        token.raise_if_cancelled()
        await self._acquire_slot(token)
        self._active.add(run_id)
        try:
            token.raise_if_cancelled()
            self._mark_status(run_id, AgentRunStatus.RUNNING)
            return await self._run_with_timeout(
                plugin, context, token, timeout_s
            )
        finally:
            self._active.discard(run_id)
            self._semaphore.release()

    async def _acquire_slot(self, token: CancellationToken) -> None:
        """竞争获取并发槽位；取消信号先完成则中止排队并释放已得槽位。"""
        acquire_task = asyncio.create_task(self._semaphore.acquire())
        cancel_task = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
                {acquire_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                if acquire_task.done() and acquire_task.result() is True:
                    # 取消与获取槽位竞争：槽位已到手，归还避免泄漏
                    self._semaphore.release()
                acquire_task.cancel()
                raise ExecutionCancelled("run 已取消")
        finally:
            cancel_task.cancel()
            if not acquire_task.done():
                acquire_task.cancel()

    async def _run_with_timeout(
        self,
        plugin,
        context,
        token: CancellationToken,
        timeout_s: int,
    ) -> dict:
        """对插件执行施加超时；0 或负数表示不限制。"""
        if timeout_s and timeout_s > 0:
            try:
                return await asyncio.wait_for(
                    self._run_plugin(plugin, context, token),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError as exc:
                raise ExecutionTimedOut(
                    f"run 执行超时: {timeout_s}s"
                ) from exc
        return await self._run_plugin(plugin, context, token)

    async def _run_plugin(self, plugin, context, token: CancellationToken) -> dict:
        """执行插件；取消信号与插件任务竞争，取消优先。"""
        plugin_task = asyncio.create_task(plugin.execute(context))
        cancel_task = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
                {plugin_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                plugin_task.cancel()
                raise ExecutionCancelled("run 已取消")
            try:
                return await plugin_task
            except asyncio.CancelledError as exc:
                raise ExecutionCancelled("run 已取消") from exc
        finally:
            cancel_task.cancel()
            if not plugin_task.done():
                plugin_task.cancel()

    # -- 账本回写 -----------------------------------------------------------

    def _mark_status(self, run_id: str, status: AgentRunStatus) -> None:
        run = self._ledger.get_run(run_id)
        if run is not None:
            run.status = status
            self._ledger.update_run(run)

    def _finalize(
        self,
        run_id: str,
        status: AgentRunStatus,
        summary: dict,
        error: str,
    ) -> ExecutionResult:
        run = self._ledger.get_run(run_id)
        if run is not None:
            run.status = status
            run.result_summary = dict(summary or {})
            if status is AgentRunStatus.CANCELLED:
                run.cancelled = True
            self._ledger.update_run(run)
        return ExecutionResult(
            run_id=run_id, status=status, summary=dict(summary or {}), error=error
        )
