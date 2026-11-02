"""远程作业执行（Phase 4 / §5.11）：**引擎管机制、插件管策略**。

* 主侧 :class:`RemoteHub`：``run_remote`` 一次调用一个协调器——选节点
  （``nodes.select``，「在途/容量」最小者优先）/ 下发（``flow.step.run``）/
  租约（``flow.step.lease`` 续租、超时判 lost）/ 收结果（``flow.step.result``
  按键幂等）/ 重试换节点 / 取消补发（``flow.step.cancel``，尽力而为）；
* worker 侧 :class:`WorkerRuntime`：收 ``flow.step.run`` 执行本地步骤
  （按 ``(run_id, node_id, attempt, job_id)`` 去重，重复投递重发上次结果；
  同键换 nonce = 被接替的投递，本地取消旧执行）；周期续租；回报结果。

协议（附录 E.2，``proto = 1``）：消息一律带完整键 ``(run_id, node_id,
attempt, job_id)`` 与 128 位 ``nonce``；主节点校验「作业在途 + 尝试号一致 +
nonce 一致 + meta.node 等于分配节点」，换节点重试后的旧结果靠 nonce 挡掉；
时长字段（``lease_s`` / ``deadline_s``）一律相对值，不传绝对时间戳。

重试语义（§5.11.5）：租约超时（lost）——步骤幂等或仍有重试预算 → 排除失联
节点换节点重试，否则结果 ``unknown`` 并打断 run（人工处置）；明确失败
（failed）——仅 ``retryable`` 且有预算才重试。一切重试受 ``timeout``
（deadline）与 ``place_wait`` 约束，不会无限循环。

投递策略是普通 dict（``comm`` 的 DeliveryPolicy 结构）——本包**不 import
comm 插件**（插件铁律），策略语义由 comm 解释。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from typing import Any

from common.flow_contracts import (
    REMOTE_PROTO,
    STEP_KIND,
    TOPIC_STEP_CANCEL,
    TOPIC_STEP_LEASE,
    TOPIC_STEP_RESULT,
    TOPIC_STEP_RUN,
    JsonValue,
    RemoteJobState,
    RemoteResult,
    StepContext,
    StepOutput,
    StepRetryable,
    StepSkipped,
)
from common.log_op import log_operation

from .config import FlowConfig
from .registry import StepRecord

#: worker 去重 / 重放表上限（终态条目超出按最旧淘汰）
_WORKER_MEMORY: int = 256

#: 协调器兜底轮询间隔（秒）；结果 / 续租到达时经 _wake 立即唤醒
_TICK: float = 0.25

#: 投递策略（普通 dict；语义见 comm 的 DeliveryPolicy）
_POLICY_FANF: dict[str, int] = {"min_success": 0, "max_attempts": 1}
_POLICY_ONCE: dict[str, int] = {"min_success": 1, "min_retries": 0, "max_attempts": 0}
_POLICY_RUN: dict[str, int] = {"min_success": 1, "min_retries": 2, "max_attempts": 5}
_POLICY_CANCEL: dict[str, int] = {"min_success": 0, "max_attempts": 3}


def _key_of(payload: Mapping[str, Any]) -> tuple[str, str, int, str]:
    """消息键：``(run_id, node_id, attempt, job_id)``（附录 E.2 校验规则）。"""
    return (
        str(payload.get("run_id") or ""),
        str(payload.get("node_id") or ""),
        int(payload.get("attempt") or 0),
        str(payload.get("job_id") or ""),
    )


def _payload_bytes(
    params: Mapping[str, JsonValue], input_data: Mapping[str, JsonValue]
) -> int:
    return len(
        json.dumps(
            {"params": dict(params), "input": dict(input_data)}, ensure_ascii=False
        ).encode("utf-8")
    )


# --------------------------------------------------------------------------- #
# worker 侧
# --------------------------------------------------------------------------- #

class WorkerRuntime:
    """worker 侧远程步骤执行器（comm topic：run / cancel；上行 lease / result）。"""

    def __init__(
        self,
        *,
        comm: Any,
        records: Callable[[], list[StepRecord]],
        config: FlowConfig,
        node_name: str,
        logger: Any,
    ) -> None:
        self._comm = comm
        self._records = records
        self._config = config
        self._node_name = node_name
        self._logger = logger
        self._jobs: dict[tuple[str, str, int, str], dict[str, Any]] = {}
        self._disposers = [
            comm.on(TOPIC_STEP_RUN, self._on_run),
            comm.on(TOPIC_STEP_CANCEL, self._on_cancel),
        ]

    def close(self) -> None:
        """注销处理器并取消在途远程尝试（引擎卸载）。"""
        for dispose in self._disposers:
            with suppress(Exception):
                dispose()
        self._disposers.clear()
        for entry in list(self._jobs.values()):
            for name in ("task", "lease_task"):
                task = entry.get(name)
                if isinstance(task, asyncio.Task):
                    task.cancel()

    # ------------------------------------------------------------- 消息入口
    async def _on_run(self, payload: Any, meta: Any) -> None:
        if not isinstance(payload, dict):
            return
        data = _validate_run(payload)
        if data is None:
            self._logger.warning("flow 远程 run 消息不合协议，已丢弃：%r", payload)
            return
        key = _key_of(payload)
        known = self._jobs.get(key)
        if known is not None:
            if known["nonce"] == data["nonce"]:
                if known.get("result") is not None:
                    self._report(known["result"])  # 重复投递：重发上次结果
                return  # 在途：忽略
            # 同键不同 nonce = 被接替的投递（主节点换节点 / 重派）：本地取消旧执行
            self._cancel_local(known)
        self._prune()
        entry: dict[str, Any] = {
            "nonce": data["nonce"],
            "result": None,
            "task": None,
            "lease_task": None,
            "sc": None,
        }
        self._jobs[key] = entry
        entry["task"] = asyncio.create_task(self._execute(key, data, entry))

    async def _on_cancel(self, payload: Any, meta: Any) -> None:
        if not isinstance(payload, dict):
            return
        entry = self._jobs.get(_key_of(payload))
        if entry is None:
            return
        if entry.get("result") is not None:
            self._report(entry["result"])  # 已完成：重发结果（对端可能没收到）
            return
        self._cancel_local(entry)

    # ------------------------------------------------------------- 执行
    async def _execute(
        self,
        key: tuple[str, str, int, str],
        data: Mapping[str, Any],
        entry: dict[str, Any],
    ) -> None:
        record = self._find_record(str(data["stage"]), str(data["step_id"]))
        if record is None:
            entry["result"] = self._result(
                key, data, "failed",
                error=f"no_step: {data['stage']}/{data['step_id']}",
                retryable=False, duration_ms=0.0,
            )
            self._report(entry["result"])
            return
        sc = StepContext(
            run_id=str(data["run_id"]),
            flow_id=str(data.get("flow_id") or ""),
            node_id=str(data["node_id"]),
            attempt=int(data["attempt"]),
            params=dict(data["params"]),
            input=dict(data["input"]),
            data={},  # 黑板不跨节点（§5.11：需要的上下文放 params）
            cancelled=asyncio.Event(),
        )
        entry["sc"] = sc
        entry["lease_task"] = asyncio.create_task(self._lease_loop(key, data, entry))
        started = time.monotonic()
        state = "failed"
        output: JsonValue = None
        error = ""
        retryable = False
        try:
            await record.acquire_slot()
            try:
                with log_operation(str(data.get("op") or data["run_id"])):
                    state, output, error, retryable = await self._invoke(
                        record, sc, float(data["deadline_s"])
                    )
            finally:
                record.release_slot()
        finally:
            lease = entry.get("lease_task")
            if isinstance(lease, asyncio.Task):
                lease.cancel()
        entry["result"] = self._result(
            key, data, state, output=output, error=error, retryable=retryable,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
        self._report(entry["result"])

    async def _invoke(
        self, record: StepRecord, sc: StepContext, deadline_s: float
    ) -> tuple[str, JsonValue, str, bool]:
        """跑 handler；超时 / 取消两段式收尾（与执行器 §5.8.4 同款）。"""
        handler = record.handler

        async def call() -> StepOutput:
            result = handler(sc)
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                return await result  # type: ignore[no-any-return]
            return result  # type: ignore[no-any-return]

        task: asyncio.Task[StepOutput] = asyncio.create_task(call())
        waiter = asyncio.create_task(sc.cancelled.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, waiter},
                timeout=deadline_s if deadline_s > 0 else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not waiter.done():
                waiter.cancel()
        if task in done:
            return self._outcome(record, task)
        timed_out = waiter not in done
        sc.cancelled.set()
        done2, _pending2 = await asyncio.wait({task}, timeout=self._config.cancel_grace)
        if task not in done2:
            task.cancel()
            with suppress(BaseException):
                await task
        return ("failed", None, "timeout" if timed_out else "cancelled", timed_out)

    def _outcome(
        self, record: StepRecord, task: asyncio.Task[StepOutput]
    ) -> tuple[str, JsonValue, str, bool]:
        try:
            output = task.result()
        except StepSkipped as exc:
            return ("skipped", None, str(exc) or "skipped", False)
        except StepRetryable as exc:
            return ("failed", None, str(exc) or "retryable", True)
        except TimeoutError as exc:
            return ("failed", None, str(exc) or "timeout", True)
        except asyncio.CancelledError:
            return ("cancelled", None, "cancelled", False)
        except Exception as exc:  # noqa: BLE001 —— 步骤异常一律折算结局
            return ("failed", None, f"{type(exc).__name__}: {exc}", record.idempotent)
        if output is None:
            return ("succeeded", None, "", False)
        if not isinstance(output, dict):
            return ("failed", None, "输出不是 JSON 对象", False)
        size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))
        if size > self._config.max_remote_payload_bytes:
            return ("failed", None, "output_too_large", False)
        return ("succeeded", dict(output), "", False)

    # ------------------------------------------------------------- 上行
    async def _lease_loop(
        self,
        key: tuple[str, str, int, str],
        data: Mapping[str, Any],
        entry: dict[str, Any],
    ) -> None:
        run_id, node_id, attempt, job_id = key
        while True:
            await asyncio.sleep(self._config.lease_interval)
            if entry.get("result") is not None:
                return
            self._comm.report(
                TOPIC_STEP_LEASE,
                {
                    "proto": REMOTE_PROTO, "run_id": run_id, "node_id": node_id,
                    "attempt": attempt, "job_id": job_id, "nonce": entry["nonce"],
                },
                policy=_POLICY_FANF,
                immediate=True,
            )

    def _result(
        self,
        key: tuple[str, str, int, str],
        data: Mapping[str, Any],
        state: str,
        *,
        output: JsonValue = None,
        error: str = "",
        retryable: bool = False,
        duration_ms: float = 0.0,
    ) -> dict[str, Any]:
        run_id, node_id, attempt, job_id = key
        return {
            "proto": REMOTE_PROTO, "run_id": run_id, "node_id": node_id,
            "attempt": attempt, "job_id": job_id, "nonce": str(data["nonce"]),
            "state": state, "output": output, "error": error or None,
            "retryable": retryable, "duration_ms": duration_ms,
        }

    def _report(self, result: Mapping[str, Any]) -> None:
        self._comm.report(TOPIC_STEP_RESULT, dict(result), policy=_POLICY_ONCE)

    def _find_record(self, stage: str, step_id: str) -> StepRecord | None:
        for record in self._records():
            if record.alive and record.stage == stage and record.step_id == step_id:
                return record
        return None

    def _cancel_local(self, entry: dict[str, Any]) -> None:
        sc = entry.get("sc")
        if sc is not None:
            sc.cancelled.set()
        task = entry.get("task")
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()

    def _prune(self) -> None:
        if len(self._jobs) < _WORKER_MEMORY:
            return
        finished = [
            key for key, entry in self._jobs.items() if entry.get("result") is not None
        ]
        for key in finished[: max(0, len(self._jobs) - _WORKER_MEMORY + 1)]:
            self._jobs.pop(key, None)


def _validate_run(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """``flow.step.run`` 协议校验（附录 E.2）；不合格返回 ``None``。"""
    if int(payload.get("proto") or 0) != REMOTE_PROTO:
        return None
    try:
        data = {
            "run_id": str(payload["run_id"]),
            "flow_id": str(payload.get("flow_id") or ""),
            "node_id": str(payload["node_id"]),
            "attempt": int(payload["attempt"]),
            "job_id": str(payload["job_id"]),
            "nonce": str(payload["nonce"]),
            "stage": str(payload["stage"]),
            "step_id": str(payload["step_id"]),
            "params": dict(payload.get("params") or {}),
            "input": dict(payload.get("input") or {}),
            "deadline_s": float(payload.get("deadline_s") or 0.0),
            "op": str(payload.get("op") or ""),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if not data["run_id"] or not data["node_id"] or not data["job_id"] or not data["nonce"]:
        return None
    return data


# --------------------------------------------------------------------------- #
# 主侧
# --------------------------------------------------------------------------- #

class _Job:
    """主侧一个远程作业的在途状态（一次 ``run_remote`` 调用内的分片）。"""

    __slots__ = (
        "attempts",
        "avoid",
        "dispatched_at",
        "error",
        "excluded",
        "finished",
        "finished_at",
        "flow_node_id",
        "idempotent",
        "job_id",
        "lease_deadline",
        "node",
        "nonce",
        "output",
        "params",
        "placement_since",
        "retries",
        "run_id",
        "state",
        "step_id",
    )

    def __init__(
        self,
        job_id: str,
        params: Mapping[str, JsonValue],
        *,
        run_id: str,
        flow_node_id: str,
    ) -> None:
        self.job_id = job_id
        self.params = dict(params)
        self.run_id = run_id
        self.flow_node_id = flow_node_id
        self.attempts = 0
        self.retries = 0
        self.nonce = ""
        self.node = ""
        self.step_id = ""
        self.idempotent = False
        self.excluded: set[str] = set()  # 硬排除（失联节点，不再回来）
        self.avoid: set[str] = set()  # 软避让（失败节点；无备选时仍可重试）
        self.state: RemoteJobState | None = None
        self.output: JsonValue = None
        self.error = ""
        self.lease_deadline = 0.0
        self.dispatched_at = 0.0
        self.finished_at = 0.0
        self.placement_since: float | None = None
        self.finished = asyncio.Event()

    def duration_ms(self) -> float:
        if not self.dispatched_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return (end - self.dispatched_at) * 1000.0


class _Call:
    """一次 ``run_remote`` 调用的上下文（策略参数 + 作业集）。"""

    __slots__ = ("deadline", "input", "jobs", "max_parallel", "retries", "sc", "stage", "where")

    def __init__(
        self,
        *,
        sc: StepContext,
        stage: str,
        where: dict[str, str],
        input: dict[str, JsonValue],
        max_parallel: int,
        retries: int,
        deadline: float,
        jobs: list[_Job],
    ) -> None:
        self.sc = sc
        self.stage = stage
        self.where = where
        self.input = input
        self.max_parallel = max_parallel
        self.retries = retries
        self.deadline = deadline
        self.jobs = jobs


class RemoteHub:
    """主侧远程作业协调器（job 表跨调用共享；``run`` 一次调用一个协调循环）。"""

    def __init__(
        self,
        *,
        comm: Any,
        nodes_getter: Callable[[], Any],
        config: FlowConfig,
        node_name: str,
        logger: Any,
        on_unknown: Callable[[str, str, str, str], Awaitable[None]],
        stats: dict[str, int],
    ) -> None:
        self._comm = comm
        self._nodes_getter = nodes_getter
        self._config = config
        self._node_name = node_name
        self._logger = logger
        self._on_unknown = on_unknown
        self._stats = stats
        self._jobs: dict[str, _Job] = {}
        self._wake = asyncio.Event()
        self._disposers = [
            comm.on(TOPIC_STEP_RESULT, self._on_result),
            comm.on(TOPIC_STEP_LEASE, self._on_lease),
        ]

    def close(self) -> None:
        for dispose in self._disposers:
            with suppress(Exception):
                dispose()
        self._disposers.clear()

    # ------------------------------------------------------------- 入口
    async def run(
        self,
        sc: StepContext,
        *,
        stage: str,
        where: Mapping[str, str] | None,
        jobs: Sequence[Mapping[str, JsonValue]],
        max_parallel: int,
        retries: int,
        timeout: float,
    ) -> list[RemoteResult]:
        """§5.11.3 远程作业原语：机制在此，策略在调用方。"""
        input_data = dict(sc.input)
        created: list[_Job] = []
        for params in jobs:
            job = _Job(
                f"{sc.node_id}#{secrets.token_hex(6)}", params,
                run_id=sc.run_id, flow_node_id=sc.node_id,
            )
            job.retries = max(0, retries)
            if _payload_bytes(params, input_data) > self._config.max_remote_payload_bytes:
                self._finalize(job, RemoteJobState.FAILED, error="payload_too_large")
            created.append(job)
        self._stats["remote_jobs"] = self._stats.get("remote_jobs", 0) + len(created)
        ctx = _Call(
            sc=sc, stage=stage, where=dict(where or {}), input=input_data,
            max_parallel=max(1, max_parallel), retries=max(0, retries),
            deadline=time.monotonic() + max(0.1, timeout), jobs=created,
        )
        for job in created:
            if job.state is None:
                self._jobs[job.job_id] = job
        try:
            await self._coordinate(ctx)
        finally:
            for job in created:
                self._jobs.pop(job.job_id, None)
        return [
            RemoteResult(
                job_id=job.job_id,
                state=job.state or RemoteJobState.CANCELLED,
                output=job.output,
                error=job.error,
                node=job.node,
                attempts=job.attempts,
                duration_ms=job.duration_ms(),
            )
            for job in created
        ]

    # ------------------------------------------------------------- 协调循环
    async def _coordinate(self, ctx: _Call) -> None:
        while True:
            now = time.monotonic()
            for job in self._reap(ctx, now):
                # unknown 处置就地 await（§5.11.5）：run_remote 返回前打断标记就位
                self._logger.error(
                    "flow 远程作业 %s 失联且不可安全重试，run %s 打断", job.job_id, job.run_id
                )
                await self._on_unknown(
                    job.run_id, job.flow_node_id, job.job_id, "remote_lease_lost"
                )
            self._place(ctx, now)
            if all(job.state is not None for job in ctx.jobs):
                return
            if now >= ctx.deadline:
                for job in ctx.jobs:
                    if job.state is None:
                        self._finalize(job, RemoteJobState.FAILED, error="timeout")
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_TICK)
            except TimeoutError:
                pass
            self._wake.clear()

    def _reap(self, ctx: _Call, now: float) -> list[_Job]:
        """租约超时判 lost；deadline 判 timeout；返回需打断 run 的 unknown 作业。"""
        unknown: list[_Job] = []
        for job in ctx.jobs:
            if job.state is not None:
                continue
            if job.node and job.lease_deadline and now > job.lease_deadline:
                if self._lost(job):
                    unknown.append(job)
            elif not job.node and now >= ctx.deadline:
                self._finalize(job, RemoteJobState.FAILED, error="timeout")
        return unknown

    def _place(self, ctx: _Call, now: float) -> None:
        """排队作业选节点下发；无候选按 place_wait 计时（§5.11.8）。"""
        inflight = sum(1 for job in ctx.jobs if job.state is None and job.node)
        for job in ctx.jobs:
            if job.state is not None or job.node:
                continue
            if inflight >= ctx.max_parallel:
                return
            candidates = self._select(ctx, job)
            if not candidates:
                if job.placement_since is None:
                    job.placement_since = now
                elif now - job.placement_since >= self._config.place_wait:
                    self._finalize(job, RemoteJobState.FAILED, error="no_capable_node")
                continue
            job.placement_since = None
            preferred = [
                cap for cap in candidates if str(cap.get("node_id") or "") not in job.avoid
            ]
            pick = min(
                preferred or candidates,
                key=lambda cap: (
                    self._node_load(str(cap.get("node_id") or "")) / _capacity(cap),
                    str(cap.get("node_id") or ""),
                ),
            )
            self._dispatch(ctx, job, pick, now)
            inflight += 1

    def _select(self, ctx: _Call, job: _Job) -> list[dict[str, Any]]:
        nodes = self._nodes_getter()
        if nodes is None:
            return []
        wanted = {"stage": ctx.stage, **ctx.where}
        try:
            caps = nodes.select(kinds=[STEP_KIND], labels=wanted, online_only=True)
        except Exception as exc:  # noqa: BLE001 —— nodes 故障不拖垮编排
            self._logger.error("flow 远程选节点失败：%s", exc)
            return []
        picked: list[dict[str, Any]] = []
        for cap in caps:
            node_id = str(cap.get("node_id") or "")
            if not node_id or node_id == self._node_name or node_id in job.excluded:
                continue
            picked.append(dict(cap))
        return picked

    def _dispatch(self, ctx: _Call, job: _Job, cap: Mapping[str, Any], now: float) -> None:
        step_id = str(cap.get("id") or "").split("/", 1)[-1]
        detail = cap.get("detail") if isinstance(cap.get("detail"), dict) else {}
        job.attempts += 1
        job.node = str(cap.get("node_id") or "")
        job.step_id = step_id
        job.idempotent = bool(detail.get("idempotent"))
        job.nonce = secrets.token_hex(16)  # 128 位随机（附录 E.2）
        job.dispatched_at = job.dispatched_at or now
        job.lease_deadline = now + self._config.lease_timeout
        self._stats["remote_dispatched"] = self._stats.get("remote_dispatched", 0) + 1
        self._comm.send_to(
            job.node,
            TOPIC_STEP_RUN,
            {
                "proto": REMOTE_PROTO,
                "run_id": job.run_id,
                "flow_id": ctx.sc.flow_id,
                "node_id": job.flow_node_id,
                "attempt": job.attempts,
                "job_id": job.job_id,
                "nonce": job.nonce,
                "stage": ctx.stage,
                "step_id": step_id,
                "params": dict(job.params),
                "input": dict(ctx.input),
                "lease_s": self._config.lease_timeout,
                "deadline_s": max(0.0, ctx.deadline - now),
                "op": job.run_id,
            },
            policy=_POLICY_RUN,
        )

    # ------------------------------------------------------------- 结果处理
    async def _on_result(self, payload: Any, meta: Any) -> None:
        job = self._verify(payload, meta)
        if job is None:
            return
        state = str(payload.get("state") or "")
        error = str(payload.get("error") or "")
        if state == "succeeded":
            self._finalize(job, RemoteJobState.SUCCEEDED, output=payload.get("output"))
        elif state == "skipped":
            self._finalize(job, RemoteJobState.SKIPPED, error=error)
        elif state == "cancelled":
            self._finalize(job, RemoteJobState.CANCELLED, error=error)
        elif state == "failed":
            self._failed(job, error, retryable=bool(payload.get("retryable")))
        # 其余状态：不合状态机，忽略（§5.11.6 乱序容忍）

    async def _on_lease(self, payload: Any, meta: Any) -> None:
        job = self._verify(payload, meta)
        if job is None or job.state is not None:
            return  # 终态后的 lease 直接忽略（§5.11.6）
        job.lease_deadline = time.monotonic() + self._config.lease_timeout

    def _verify(self, payload: Any, meta: Any) -> _Job | None:
        """结果 / 续租校验：作业在途 + 尝试号一致 + nonce 一致 + 节点一致。"""
        if not isinstance(payload, dict) or int(payload.get("proto") or 0) != REMOTE_PROTO:
            return None
        job = self._jobs.get(str(payload.get("job_id") or ""))
        if job is None or job.state is not None:
            return None  # 迟到 / 重复：幂等丢弃（§5.11.6）
        if int(payload.get("attempt") or 0) != job.attempts:
            return None  # 旧尝试的结果
        if str(payload.get("nonce") or "") != job.nonce:
            return None  # 过期投递（换节点重试的旧结果）
        sender = str((meta or {}).get("node") or "")
        if sender and sender != job.node:
            self._logger.warning("flow 远程消息节点不符：%s != %s", sender, job.node)
            return None
        return job

    def _failed(self, job: _Job, error: str, *, retryable: bool) -> None:
        if retryable and job.attempts <= job.retries:
            self._stats["remote_retried"] = self._stats.get("remote_retried", 0) + 1
            self._requeue(job, soft=True)  # 软避让失败节点：有备选先换人
        else:
            self._finalize(job, RemoteJobState.FAILED, error=error or "failed")

    def _lost(self, job: _Job) -> bool:
        """租约超时（§5.11.5）：幂等或有预算 → 排除失联节点换节点重试。

        返回 ``True`` 表示结果 ``unknown``、需由调用方打断 run。
        """
        self._stats["remote_lost"] = self._stats.get("remote_lost", 0) + 1
        self._send_cancel(job, "lease_lost")
        if job.idempotent or job.attempts <= job.retries:
            self._stats["remote_retried"] = self._stats.get("remote_retried", 0) + 1
            self._requeue(job, hard=True)  # 失联节点硬排除（§5.11.5 排除失联节点）
            return False
        self._stats["remote_unknown"] = self._stats.get("remote_unknown", 0) + 1
        self._finalize(job, RemoteJobState.UNKNOWN, error="lease_lost")
        return True

    def _requeue(self, job: _Job, *, hard: bool = False, soft: bool = False) -> None:
        if hard and job.node:
            job.excluded.add(job.node)
        if soft and job.node:
            job.avoid.add(job.node)
        job.node = ""
        job.step_id = ""
        job.nonce = ""
        job.lease_deadline = 0.0
        self._wake.set()

    def _finalize(
        self,
        job: _Job,
        state: RemoteJobState,
        *,
        output: JsonValue = None,
        error: str = "",
    ) -> None:
        if job.state is not None:
            return
        job.state = state
        job.output = output
        job.error = error
        job.finished_at = time.monotonic()
        job.finished.set()
        self._wake.set()

    def _send_cancel(self, job: _Job, reason: str) -> None:
        if not job.node:
            return
        with suppress(Exception):
            self._comm.send_to(
                job.node,
                TOPIC_STEP_CANCEL,
                {
                    "proto": REMOTE_PROTO, "run_id": job.run_id,
                    "node_id": job.flow_node_id, "attempt": job.attempts,
                    "job_id": job.job_id, "reason": reason,
                },
                policy=_POLICY_CANCEL,
            )

    def _node_load(self, node_id: str) -> int:
        return sum(
            1 for job in self._jobs.values() if job.state is None and job.node == node_id
        )


def _capacity(cap: Mapping[str, Any]) -> float:
    detail = cap.get("detail") if isinstance(cap.get("detail"), dict) else {}
    raw = detail.get("max_concurrency")
    return float(raw) if isinstance(raw, (int, float)) and raw > 0 else 1.0


__all__: list[str] = ["RemoteHub", "WorkerRuntime"]
