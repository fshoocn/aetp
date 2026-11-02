"""运行执行器（Phase 3 持久化版；§5.8 执行语义 + §5.10 记录挂点）。

* run 终态规则（§5.8.1）：任一**必选**节点失败 → ``failed``；取消且无必选失败
  → ``cancelled``；否则 ``succeeded``（可选节点的失败 / 缺失记 ``warnings``）；
* 重试（§5.8.4）：``StepRetryable`` / ``TimeoutError`` / **幂等**步骤的普通异常；
  退避 ``backoff * 2^(attempt-1)``（上限 ``retry_cap``）；
* 超时与取消共用两段式收尾：置 ``sc.cancelled`` → ``cancel_grace`` 协作退出
  → 仍不退出才 ``task.cancel()``（§5.4 / §5.8.4）；
* 有序拆卸（§5.12）：排空的强制收尾也走本收尾逻辑（drain 只发信号 + 等待）；
* 持久化（§5.10）：生命周期各点先经 ``recorder`` 落库（先写后做）；记录异常
  按 ``store_error`` 快速失败（run failed + 在途尝试协作取消）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol, cast

from common.flow_contracts import (
    EVT_NODE_FINISHED,
    EVT_RUN_FINISHED,
    EVT_RUN_STARTED,
    JsonValue,
    Mode,
    NodeDef,
    NodeState,
    Overrides,
    ProblemCode,
    RunState,
    StepContext,
    StepOutput,
    StepRetryable,
    StepSkipped,
)
from common.log_op import log_operation

from .config import FlowConfig
from .registry import Attempt, StepRecord
from .resolver import no_provider_problem, resolve_node


@dataclass(slots=True)
class NodeRun:
    """一个节点的运行记录（内存态；Phase 3 落库为 ``aetp_flow_steps`` 投影）。"""

    node: NodeDef
    state: NodeState = NodeState.PENDING
    provider: str = ""
    step: str = ""
    attempts: int = 0
    output: JsonValue = None
    error: str = ""
    reason: str = ""
    exec_node: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass(slots=True)
class Run:
    """一次运行（内存态；黑板 = 各节点最新成功输出）。"""

    id: str
    flow_id: str
    flow_version: int
    input: dict[str, JsonValue]
    overrides: Overrides | None
    nodes: tuple[NodeDef, ...]
    disabled: frozenset[str] = frozenset()
    initiator: str = ""
    state: RunState = RunState.PENDING
    warnings: list[str] = field(default_factory=list[str])
    error: str = ""
    idempotency_key: str = ""
    data: dict[str, JsonValue] = field(default_factory=dict[str, JsonValue])
    node_runs: dict[str, NodeRun] = field(default_factory=dict[str, NodeRun])
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = 0.0
    finished_at: float = 0.0
    #: 绑定留痕（node → {provider, step, version, exec_node}；§5.6，落 runs.bindings）
    bindings: dict[str, dict[str, object]] = field(default_factory=dict)
    #: attempt 序号接续（恢复：从 steps 投影回填，序号不重头；§5.10）
    attempt_offsets: dict[str, int] = field(default_factory=dict)
    #: 远程未知失联（§5.11.5）：打断原因 + 应记 unknown 的节点（run 记 interrupted）
    interrupted_reason: str = ""
    unknown_node_ids: set[str] = field(default_factory=set)
    #: 在途尝试现场（按 id(sc) 索引——StepContext 含 dict 字段不可哈希）
    inflight: dict[int, StepContext] = field(default_factory=dict[int, StepContext])

    def __post_init__(self) -> None:
        for node in self.nodes:
            self.node_runs[node.id] = NodeRun(node)

    def duration_ms(self) -> int:
        end = self.finished_at or time.monotonic()
        return int((end - self.started_at) * 1000)


@dataclass(slots=True)
class _Outcome:
    """一次尝试的结局（kind：ok / skipped / retryable / failed / cancelled / timeout）。"""

    kind: str
    output: StepOutput = None
    error: str = ""


class RunRecorder(Protocol):
    """run 记录器（Phase 3：``FlowStore`` 落库即实现；None = 仅内存）。

    「先写后做 / 做完必记」（§5.10）：记录异常向上传播，由
    :meth:`FlowExecutor.execute` 按 ``store_error`` 快速失败处置。
    """

    async def record_run_started(self, run: Run, *, actor: str = "") -> None: ...

    async def record_attempt_started(
        self, run: Run, node_id: str, attempt: int, record: StepRecord, exec_node: str
    ) -> None: ...

    async def record_attempt_finished(
        self, run: Run, node_id: str, attempt: int, *,
        state: str, output: StepOutput = None, error: str = "", duration_ms: float = 0.0,
    ) -> None: ...

    async def record_node_finished(self, run: Run, node_id: str, nr: NodeRun) -> None: ...

    async def record_run_finished(self, run: Run) -> None: ...


class FlowExecutor:
    """执行一个 :class:`Run`（不持有全局状态；并发由 service 的名额控制）。"""

    def __init__(
        self,
        records: Callable[[], list[StepRecord]],
        config: FlowConfig,
        *,
        dispatch: Callable[[str, dict[str, object]], Awaitable[None]],
        node_name: str,
        recorder: RunRecorder | None = None,
        stats: dict[str, int] | None = None,
    ) -> None:
        self._records = records
        self._config = config
        self._dispatch = dispatch
        self._node_name = node_name
        self._recorder = recorder
        self._stats = stats

    def _bump(self, key: str) -> None:
        if self._stats is not None:
            self._stats[key] = self._stats.get(key, 0) + 1

    # ------------------------------------------------------------- run 主流程
    async def execute(self, run: Run) -> None:
        """依序执行节点（run 已获得并发名额），结束后派发 ``flow/run-finished``。

        Phase 3：生命周期各点先经 ``recorder`` 落库（§5.10「先写后做」）；
        持久化等基础设施异常按 ``store_error`` 快速失败。
        """
        with log_operation(run.id):
            try:
                await self._advance(run)
            except Exception as exc:  # noqa: BLE001 —— 基础设施故障快速失败（§5.10）
                run.state = RunState.FAILED
                run.error = f"store_error: {type(exc).__name__}: {exc}"
                run.cancelled.set()
                for sc in list(run.inflight.values()):
                    sc.cancelled.set()
            run.finished_at = time.monotonic()
            if self._recorder is not None:
                with suppress(Exception):  # 终局记录尽力而为（run 已到终态）
                    await self._recorder.record_run_finished(run)
            with suppress(Exception):
                await self._dispatch(
                    EVT_RUN_FINISHED,
                    {
                        "run_id": run.id,
                        "flow_id": run.flow_id,
                        "flow_version": run.flow_version,
                        "state": run.state.value,
                        "duration_ms": run.duration_ms(),
                        "warnings": list(run.warnings),
                        "initiator": run.initiator,
                    },
                )
            run.finished.set()

    async def _advance(self, run: Run) -> None:
        """节点推进主体（异常上抛，由 :meth:`execute` 按 store_error 兜住）。"""
        if self._recorder is not None:
            await self._recorder.record_run_started(run)
        run.state = RunState.RUNNING
        run.started_at = time.monotonic()
        await self._dispatch(
            EVT_RUN_STARTED,
            {
                "run_id": run.id,
                "flow_id": run.flow_id,
                "flow_version": run.flow_version,
                "initiator": run.initiator,
            },
        )
        required_failed = False
        for node in run.nodes:
            nr = run.node_runs[node.id]
            if nr.state is not NodeState.PENDING:
                # 恢复回填的已终态节点：不重跑、不重复记事件，必选失败仍计账（§5.10）
                if node.required and nr.state is NodeState.FAILED:
                    required_failed = True
                continue
            if node.id in run.disabled:
                await self._finish_node(run, node, nr, NodeState.SKIPPED, "disabled")
                continue
            if not node.always and required_failed:
                await self._finish_node(run, node, nr, NodeState.SKIPPED, "prior_failure")
                continue
            if not node.always and run.cancelled.is_set():
                await self._finish_node(run, node, nr, NodeState.CANCELLED, "cancelled")
                continue
            await self._run_node(run, node, nr)
            if run.interrupted_reason:
                break  # 远程未知失联（§5.11.5）：余下节点保持 pending，run 记 interrupted
            if node.required:
                if nr.state is NodeState.FAILED:
                    required_failed = True
                elif nr.state is NodeState.CANCELLED and not run.cancelled.is_set():
                    # 排空等外部取消导致必选未完成 → run failed（§5.12）
                    required_failed = True
        if run.interrupted_reason:
            run.state = RunState.INTERRUPTED
            run.error = run.error or run.interrupted_reason
        elif required_failed:
            run.state = RunState.FAILED
            run.error = self._collect_error(run) or "required_step_failed"
        elif run.cancelled.is_set():
            run.state = RunState.CANCELLED
        else:
            run.state = RunState.SUCCEEDED

    @staticmethod
    def _collect_error(run: Run) -> str:
        for nr in run.node_runs.values():
            if nr.state is NodeState.FAILED and nr.error:
                return nr.error
        return ""

    async def _finish_node(
        self,
        run: Run,
        node: NodeDef,
        nr: NodeRun,
        state: NodeState,
        reason: str = "",
        *,
        error: str = "",
    ) -> None:
        """落节点终态并派发 ``flow/node-finished``（§5.8.2 第 7 步）。"""
        nr.state = state
        nr.reason = reason or nr.reason
        nr.error = error or nr.error
        nr.finished_at = time.monotonic()
        payload: dict[str, object] = {
            "run_id": run.id,
            "node_id": node.id,
            "state": state.value,
            "provider": nr.provider,
            "exec_node": nr.exec_node or self._node_name,
            "attempts": nr.attempts,
            "duration_ms": int((nr.finished_at - nr.started_at) * 1000) if nr.started_at else 0,
        }
        if nr.reason:
            payload["reason"] = nr.reason
        await self._dispatch(EVT_NODE_FINISHED, payload)
        if self._recorder is not None:
            await self._recorder.record_node_finished(run, node.id, nr)

    # ------------------------------------------------------------- 节点执行
    async def _run_node(self, run: Run, node: NodeDef, nr: NodeRun) -> None:
        nr.started_at = time.monotonic()
        record = await self._resolve_with_wait(run, node, nr)
        if record is None:
            # 缺提供者 / 歧义：终态已在 _handle_missing 置好，补派 node-finished
            await self._finish_node(run, node, nr, nr.state)
            return
        nr.provider = record.provider
        nr.step = record.step_id
        nr.exec_node = self._node_name
        nr.attempts = run.attempt_offsets.get(node.id, 0)  # 恢复接续（§5.10）
        picked: list[StepRecord] = [record]
        if node.mode is Mode.ALL:
            resolution = resolve_node(self._records(), node)
            picked = list(resolution.picked) or [record]
        outputs: list[JsonValue] = []
        ok_count = 0
        skipped_count = 0
        failed = False
        cancelled = False
        for target in picked:
            kind, output, error = await self._run_provider(run, node, nr, target)
            if kind == "ok":
                ok_count += 1
                if node.mode is Mode.ALL:
                    outputs.append(dict(output) if output is not None else None)
                elif output is not None:
                    outputs.append(dict(output))
            elif kind == "skipped":
                skipped_count += 1
                nr.reason = "skipped_by_step"
            elif kind == "cancelled":
                cancelled = True
                nr.error = nr.error or error
                break
            else:
                failed = True
                nr.error = nr.error or error
                if node.required:
                    break  # 必选失败：中止本节点余下提供者
        if node.id in run.unknown_node_ids:
            # 远程未知失联（§5.11.5）：该节点记 unknown（不写黑板），run 打断
            nr.error = nr.error or "remote_lost"
            await self._finish_node(run, node, nr, NodeState.UNKNOWN, "remote_lost")
            return
        if failed:
            state = NodeState.FAILED
            if not node.required:
                run.warnings.append(f"节点 {node.id} 失败：{nr.error}")
        elif cancelled:
            state = NodeState.CANCELLED
        elif ok_count == 0 and skipped_count > 0:
            state = NodeState.SKIPPED  # 步骤主动跳过（skipped_by_step），不算失败
        else:
            state = NodeState.SUCCEEDED
            written: JsonValue = outputs if node.mode is Mode.ALL else (outputs[0] if outputs else None)
            size = len(json.dumps(written, ensure_ascii=False).encode("utf-8"))
            total = len(json.dumps({**run.data, node.id: written}, ensure_ascii=False).encode("utf-8"))
            if size > self._config.max_output_bytes or total > self._config.max_blackboard_bytes:
                state = NodeState.FAILED
                nr.error = "output_too_large"
                if not node.required:
                    run.warnings.append(f"节点 {node.id} 输出超限")
            else:
                run.data[node.id] = written
                nr.output = written
        await self._finish_node(run, node, nr, state)

    async def _resolve_with_wait(
        self, run: Run, node: NodeDef, nr: NodeRun
    ) -> StepRecord | None:
        """解析提供者；必选缺提供者时按 ``resolve_wait`` 等待出现（§5.8.2）。"""
        deadline = time.monotonic() + self._config.resolve_wait
        while True:
            resolution = resolve_node(self._records(), node)
            if resolution.problem is None:
                return resolution.picked[0]
            if resolution.problem.code is ProblemCode.AMBIGUOUS:
                self._handle_missing(run, node, nr, resolution.problem.message, code="ambiguous")
                return None
            if time.monotonic() >= deadline:
                problem = no_provider_problem(node, self._records())
                self._handle_missing(run, node, nr, problem.message, code="no_provider")
                return None
            await asyncio.sleep(0.05)

    def _handle_missing(
        self, run: Run, node: NodeDef, nr: NodeRun, message: str, *, code: str
    ) -> None:
        self._bump("resolve_misses")
        if node.required:
            nr.state = NodeState.FAILED
            nr.error = f"{code}: {message}"
        else:
            nr.state = NodeState.SKIPPED
            nr.reason = "no_provider" if code == "no_provider" else code
            run.warnings.append(f"节点 {node.id} 跳过（{code}）：{message}")

    async def _run_provider(
        self, run: Run, node: NodeDef, nr: NodeRun, record: StepRecord
    ) -> tuple[str, StepOutput, str]:
        """对单个提供者跑「尝试 + 重试」循环；返回 (结局, 输出, 错误)。

        结局：``ok`` / ``skipped`` / ``cancelled`` / ``failed``。
        """
        attempt_no = 0  # 提供者内序号（StepContext.attempt / 退避用）
        current = record
        while True:
            attempt_no += 1
            nr.attempts += 1
            outcome = await self._attempt(run, node, nr, current, attempt_no)
            if outcome.kind in ("ok", "skipped"):
                return outcome.kind, outcome.output, outcome.error
            if outcome.kind == "cancelled" and run.cancelled.is_set():
                return "cancelled", None, outcome.error or "cancelled"
            retryable = outcome.kind in ("retryable", "timeout") or (
                outcome.kind == "cancelled" and current.idempotent
            )
            if not current.alive:
                # 提供者已排空 / 卸载：换人重试（§5.12「在其他提供者上重跑」）
                resolution = resolve_node(self._records(), node)
                if resolution.problem is None:
                    current = resolution.picked[0]
                    nr.provider = current.provider
                    nr.step = current.step_id
                else:
                    return "failed", None, outcome.error or "provider_gone"
            if retryable and attempt_no <= node.retries and not run.cancelled.is_set():
                delay = min(self._config.retry_cap, node.backoff * (2 ** (attempt_no - 1)))
                await asyncio.sleep(delay)
                continue
            if outcome.kind == "cancelled":
                return "cancelled", None, outcome.error or "cancelled"
            return "failed", None, outcome.error or outcome.kind

    # --------------------------------------------------------------- 单次尝试
    async def _attempt(
        self, run: Run, node: NodeDef, nr: NodeRun, record: StepRecord, attempt_no: int
    ) -> _Outcome:
        sc = StepContext(
            run_id=run.id,
            flow_id=run.flow_id,
            node_id=node.id,
            attempt=attempt_no,
            params=dict(node.params),
            input=dict(run.input),
            data=dict(run.data),  # 只读快照
            cancelled=asyncio.Event(),
        )
        if run.cancelled.is_set():
            sc.cancelled.set()
        attempt = record.begin(sc)
        run.inflight[id(sc)] = sc
        started = time.monotonic()
        recorded: _Outcome | None = None
        try:
            if self._recorder is not None:  # 先写后做（§5.10）：attempt-started 先落库
                # 节点级尝试序号（nr.attempts）：mode=all 多提供者共享一条递增序，
                # (run_id, node_id, attempt) 主键由此唯一
                await self._recorder.record_attempt_started(
                    run, node.id, nr.attempts, record, self._node_name
                )
            await record.acquire_slot()
            try:
                timeout = node.timeout if node.timeout is not None else self._config.step_timeout
                recorded = await self._invoke(
                    record, sc, attempt, timeout if timeout and timeout > 0 else None
                )
                return recorded
            finally:
                record.release_slot()
        finally:
            run.inflight.pop(id(sc), None)
            record.end(attempt)
            if self._recorder is not None:
                await self._recorder.record_attempt_finished(
                    run, node.id, nr.attempts,
                    state={
                        "ok": "succeeded", "skipped": "skipped",
                        "cancelled": "cancelled", "unknown": "unknown",
                    }.get(
                        recorded.kind if recorded is not None else "unknown", "failed"
                    ),
                    output=recorded.output if recorded is not None else None,
                    error=recorded.error if recorded is not None else "",
                    duration_ms=(time.monotonic() - started) * 1000.0,
                )

    async def _invoke(
        self,
        record: StepRecord,
        sc: StepContext,
        attempt: Attempt,
        timeout: float | None,
    ) -> _Outcome:
        """跑 handler；超时 / 取消走两段式收尾（信号 → grace → 强制取消）。"""
        handler = record.handler

        async def call() -> StepOutput:
            result = handler(sc)
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                return await cast(Awaitable[StepOutput], result)
            return cast(StepOutput, result)

        task: asyncio.Task[StepOutput] = asyncio.create_task(call())
        attempt.task = cast("asyncio.Task[object]", task)
        waiter = asyncio.create_task(sc.cancelled.wait())
        try:
            # FIRST_COMPLETED：任一「handler 完成 / 取消信号 / 超时」即返回
            done, _pending = await asyncio.wait(
                {task, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not waiter.done():
                waiter.cancel()
        if task in done:
            return self._outcome_of(record, task)
        # 超时（waiter 也没醒）或取消信号（waiter 醒了）
        timed_out = waiter not in done
        sc.cancelled.set()
        done2, _pending2 = await asyncio.wait({task}, timeout=self._config.cancel_grace)
        if task not in done2:
            task.cancel()
            with suppress(BaseException):
                await task
        kind = "timeout" if timed_out else "cancelled"
        return _Outcome(kind, None, kind)

    def _outcome_of(self, record: StepRecord, task: asyncio.Task[StepOutput]) -> _Outcome:
        try:
            output = task.result()
        except StepSkipped as exc:
            return _Outcome("skipped", None, str(exc) or "skipped")
        except StepRetryable as exc:
            return _Outcome("retryable", None, str(exc) or "retryable")
        except TimeoutError as exc:
            return _Outcome("retryable", None, str(exc) or "timeout")
        except asyncio.CancelledError:
            return _Outcome("cancelled", None, "cancelled")
        except Exception as exc:  # noqa: BLE001 —— 步骤异常一律折算结局，不外抛
            kind = "retryable" if record.idempotent else "failed"
            return _Outcome(kind, None, f"{type(exc).__name__}: {exc}")
        if output is None:
            return _Outcome("ok", None)
        if not isinstance(output, dict):
            return _Outcome("failed", None, "输出不是 JSON 对象")
        try:
            size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            return _Outcome("failed", None, f"输出不可 JSON 序列化：{exc}")
        if size > self._config.max_output_bytes:
            return _Outcome("failed", None, "output_too_large")
        return _Outcome("ok", cast(StepOutput, output))


class RunHandleImpl:
    """契约 :class:`RunHandle` 的实现：包装内存态 :class:`Run`。"""

    def __init__(self, run: Run) -> None:
        self._run = run

    @property
    def id(self) -> str:
        return self._run.id

    @property
    def state(self) -> RunState:
        return self._run.state

    async def wait(self, timeout: float | None = None) -> RunState:
        """等待终态；超时返回当前状态（不抛，§5.9 的 ``run.wait()``）。"""
        try:
            await asyncio.wait_for(self._run.finished.wait(), timeout)
        except TimeoutError:
            pass
        return self._run.state


__all__: list[str] = ["FlowExecutor", "NodeRun", "Run", "RunHandleImpl"]
