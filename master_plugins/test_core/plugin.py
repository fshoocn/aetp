"""test_core 核心插件：状态机编排 + ``ctx.test_core`` 服务（DESIGN §5/§6/§10）。

职责：测试任务的**状态机唯一写者**——提交 / 拆分 / 派发编排、Stage 屏障、
结果聚合、租约与重试、取消 / 重跑、查询门面与 HookChain 宿主。跨节点交互
一律经 ``ctx.comm``（topic 见 :mod:`common.test_contracts`），事件一律
``ctx.parallel`` 派发（``common/event_contract.py`` 全 parallel，异常隔离）。
管理 API / 页面拆附属服务 :class:`.api_service.TestCoreApiService` /
:class:`.ui_service.TestCoreUiService`，webapi / webui 缺席不阻塞核心。

调度决策（队列 / 窗口 / 配额 / 选节点 / 租约查询 / comm 收发）在
:mod:`.scheduler`，本模块负责编排与落库；插件包内不 import 其他插件包，
共享契约只住 ``common/``。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from cordis_port import Context, EventDispatchError, Fiber, Service

from common.hook_chain import OnError
from common.test_contracts import (
    CHAIN_ARTIFACT_SINK,
    CHAIN_RESULT_TRANSFORM,
    CHAIN_TASK_TRANSFORM,
    SCHEMA_VERSION,
    TEST_PROTO,
    TOPIC_JOB_PROGRESS,
    TOPIC_JOB_RESULT,
    ArtifactRef,
    CaseResult,
    ExecutorSelector,
    InvalidTransition,
    JobDescriptor,
    JobResult,
    TaskNotFound,
    TaskSpec,
    TestCoreInterface,
)

from .api_service import TestCoreApiService
from .config import TestCoreConfig
from .hooks import HookCtx, TestHooks
from .models import (
    TestArtifact,
    TestCaseResult,
    TestJob,
    TestJobResult,
    TestStage,
    TestTask,
    TestTaskHistory,
)
from .registry import (
    JOB_FINISHED,
    TASK_FINISHED,
    ensure_valid,
    history_row,
    transition,
)
from .scheduler import MODEL_OWNER, TASK_TIMEOUT_KEY, Scheduler, project_of
from .ui_service import TestCoreUiService

# -- 事件派发名（common/event_contract.py 已登记，模式全 parallel） -------------
# tests/test_event_contract.py 的扫描器只认「字面量 / 本模块级字符串常量」，
# 从 common.test_contracts import 进来的名字解析不到派发点，故以字面量重建
# （与契约层常量同值；改名先改 common/event_contract.py 注册表）。
EVENT_TASK_CREATED = "test/task-created"
EVENT_TASK_STATE = "test/task-state"
EVENT_JOB_FINISHED = "test/job-finished"
EVENT_CASE_FINISHED = "test/case-finished"
EVENT_TASK_FINISHED = "test/task-finished"
EVENT_ARTIFACT_READY = "test/artifact-ready"
EVENT_COLLECT_TASK = "test/collect-task"

#: heartbeat/node-status 事件名（emit 模式，监听器必须同步）
_NODE_STATUS_EVENT = "heartbeat/node-status"

#: 调度循环周期（秒）：派发窗口是轻量 DB 查询，1s 粒度足够
_DISPATCH_INTERVAL: float = 1.0
#: case-finished 事件里失败明细的载荷上限（载荷 = 引用 + 摘要，禁止大对象）
_CASE_EVENT_LIMIT: int = 200

#: JobResult.status → Job 状态（timeout 归为基础设施失败，§5.2 无 timeout 目标态）
_JOB_RESULT_STATES: dict[str, str] = {
    "succeeded": "succeeded",
    "failed": "failed",
    "canceled": "canceled",
    "timeout": "failed",
}


def _new_id(prefix: str, now: float) -> str:
    """时间有序 id（``t-``/``j-``/``s-``/``h-``/``c-``/``a-`` 前缀，ULID 风格）。"""
    return f"{prefix}-{int(now * 1000):013d}-{secrets.token_hex(3)}"


def _json_dumps(value: object) -> str:
    """JSON 列写入（文本列，同 outbox payload 惯例）。"""
    return json.dumps(value, ensure_ascii=False)


def _decode_json(raw: object, default: Any = None) -> Any:
    """JSON 列读出；坏行 / 空值降级 default。已是对象（消息载荷）原样返回。"""
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _decode_dict(raw: object) -> dict[str, Any]:
    """JSON 列读出为 dict（坏行降级空对象）。"""
    data = _decode_json(raw, {})
    return dict(data) if isinstance(data, dict) else {}


def _public_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """剥离内部保留键（timeout 暂存等）后的对外参数。"""
    return {key: value for key, value in params.items() if key != TASK_TIMEOUT_KEY}


class TestCorePlugin(Service[TestCoreConfig]):
    """test-core 测试核心插件（``provide = "test_core"``，DESIGN §6）。

    ``inject`` 只声明硬依赖 appconfig / db / comm；``ctx.nodes`` 是**可选依赖**
    （从节点 / 无心跳部署缺席），访问判空、缺席时调度降级（见 Scheduler）。
    webapi / webui 不进核心依赖：管理 API / 页面拆附属服务挂载，缺席安静
    PENDING（同 heartbeat-server 模式）。
    """

    name: ClassVar[str] = "test-core"
    provide: str | None = "test_core"
    inject: ClassVar[list[str]] = ["appconfig", "db", "comm"]
    Config: ClassVar[type[TestCoreConfig]] = TestCoreConfig

    def __init__(self, ctx: Context, config: TestCoreConfig | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.test_core
        self.ctx: Context = ctx
        self.config: TestCoreConfig = (
            config if isinstance(config, TestCoreConfig) else TestCoreConfig()
        )
        #: 时钟（可注入：测试整体替换本属性，Scheduler 经 callable 晚绑定）
        self._clock: Callable[[], float] = time.time
        self._hooks: TestHooks = TestHooks(logger=ctx.logger)
        self._scheduler: Scheduler = Scheduler(
            db=ctx.db,
            comm=ctx.comm,
            config=self.config,
            clock=lambda: self._clock(),
            log=self._warn,
            nodes=lambda: self.ctx.get("nodes"),
            self_name=lambda: str(self.ctx.get("node_name") or ""),
        )
        #: heartbeat/node-status 同步监听器的投递槽（调度循环异步消费）
        self._node_events: deque[str] = deque(maxlen=256)
        self._dispatch_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._api_fiber: Fiber | None = None
        self._ui_fiber: Fiber | None = None
        self._results_duplicate_total: int = 0
        self._late_results_total: int = 0
        #: /healthz 仪表缓存（扫描循环定期刷新，health_metrics 是同步接口）
        self._gauge_inflight_jobs: int = 0
        self._gauge_running_tasks: int = 0

    # ================================================================ 生命周期
    async def init(self) -> object:
        """建表 → 重启恢复 → 收件 / 事件登记 → 启动调度与租约循环；返回清理函数。"""
        # 模型归属 __owner__ = 插件 id "test-core"，与本 fiber 身份一致（生产取
        # 合成包解码的插件 id、开发取 fiber 名），归属校验自然通过
        await self.ctx.db.sync_schema(
            models=[
                TestTask,
                TestStage,
                TestJob,
                TestJobResult,
                TestCaseResult,
                TestArtifact,
                TestTaskHistory,
            ]
        )
        await self._recover()
        self.ctx.comm.on(TOPIC_JOB_RESULT, self._on_job_result)
        self.ctx.comm.on(TOPIC_JOB_PROGRESS, self._on_job_progress)
        # node-status 是 emit 事件：监听器必须同步（只投递 deque，调度循环消费）
        self.ctx.effect(
            lambda: self.ctx.on(_NODE_STATUS_EVENT, self._on_node_status),
            "test-core.node-status",
        )
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._dispatch_task.add_done_callback(self._log_task_exit)
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        self._sweep_task.add_done_callback(self._log_task_exit)
        # 附属服务不 await：webapi / webui 缺席时 fiber 停在 PENDING，晚到自动挂载
        self._api_fiber = self.ctx.plugin(TestCoreApiService)
        self._ui_fiber = self.ctx.plugin(TestCoreUiService)
        await self._refresh_gauges()
        self.ctx.logger.info(
            "test-core 已启动：租约 %s 秒 / 最大尝试 %s 次 / 派发窗口 %s",
            self.config.lease_timeout,
            self.config.max_attempts,
            self.config.max_inflight_jobs,
        )
        return self._stop

    async def _stop(self) -> None:
        """卸载清理：取消调度 / 租约循环并等待退出（gather 吸收自身取消）。"""
        tasks = [task for task in (self._dispatch_task, self._sweep_task) if task is not None]
        self._dispatch_task = None
        self._sweep_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """后台任务退出的观察哨：意外退出记 error，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("test-core 后台任务意外退出：%s", error)

    async def _recover(self) -> None:
        """重启恢复：queued 回内存队列；过期租约走 lost 路径；半途任务收敛。"""
        now = self._clock()
        jobs = await self._repo(TestJob).list()
        tasks = {task.id: task for task in await self._repo(TestTask).list()}
        queued: list[tuple[TestTask, TestJob]] = []
        for job in jobs:
            task = tasks.get(job.task_id)
            if task is None:
                continue
            if job.state == "queued":
                queued.append((task, job))
            elif job.state in ("leased", "running") and self._scheduler.lease_expired(
                job, now=now, lease_timeout=self.config.lease_timeout
            ):
                await self._handle_lost(job, reason="lease timeout on restart")
        # queued 回内存队列：priority 降序、created_ts 升序（同任务内按 job id 时序）
        queued.sort(key=lambda item: (-item[0].priority, item[0].created_ts, item[1].id))
        for task, job in queued:
            self._enqueue(job, task, created_ts=task.created_ts, avoid_node=job.node_id)
        for task in tasks.values():
            fresh = await self._repo(TestTask).get(task.id)
            if fresh is None or fresh.state not in ("pending", "planning", "running", "canceling"):
                continue
            rows = await self._job_rows(fresh.id)
            in_flight = [
                job for job in rows if job.state in ("queued", "leased", "running", "lost")
            ]
            if fresh.state == "pending":
                await self._start_first_stage(fresh)
            elif not in_flight:
                if not rows:
                    await self._resume_stage(fresh)
                elif fresh.state == "canceling":
                    await self._finish_cancel(fresh)
                else:
                    await self._evaluate_barrier(fresh.id)
            elif fresh.state == "canceling":
                for job in rows:
                    if job.state == "queued":
                        await self._cancel_queued_job(job, reason="task canceling")
        self._warn("重启恢复完成")

    # ============================================================== 提交与生命周期
    async def submit(self, spec: TaskSpec, *, source: str = "api") -> str:
        """提交任务，返回 task_id；idempotency_key 命中返回既有任务。"""
        ensure_valid(spec)
        now = self._clock()
        if spec.idempotency_key:
            existing = await self._repo(TestTask).list(
                where=(TestTask.source == source)
                & (TestTask.idempotency_key == spec.idempotency_key)
            )
            if existing:
                return existing[0].id
        # task.transform：落库前执行，以变换后 spec 落库（DESIGN §11.2）
        hook_ctx = HookCtx(chain=CHAIN_TASK_TRANSFORM, spec=spec, meta={"source": source})
        hook_ctx = await self._hooks.run_task_transform(hook_ctx)
        spec = hook_ctx.spec if hook_ctx.spec is not None else spec
        ensure_valid(spec)

        labels = dict(spec.labels or {})
        params = dict(spec.params or {})
        if spec.timeout_s > 0:
            params[TASK_TIMEOUT_KEY] = float(spec.timeout_s)  # 模型缺口：暂存进 params
        task_id = _new_id("t", now)
        await self._repo(TestTask).add(
            TestTask(
                id=task_id,
                idempotency_key=spec.idempotency_key,
                source=source,
                name=spec.name,
                created_by=source,
                priority=int(spec.priority),
                labels=_json_dumps(labels),
                params=_json_dumps(params),
                state="pending",
                current_stage="",
                failure_policy=spec.failure_policy,
                error="",
                created_ts=now,
                updated_ts=now,
                finished_ts=0.0,
            )
        )
        stages = list(spec.stages)
        for seq, stage_spec in enumerate(stages, start=1):
            await self._repo(TestStage).add(
                TestStage(
                    id=_new_id("s", now),
                    task_id=task_id,
                    seq=seq,
                    name=stage_spec.name,
                    kind="run",
                    executor=_json_dumps(
                        {
                            "id": stage_spec.executor.id,
                            "kind": stage_spec.executor.kind,
                            "labels": dict(stage_spec.executor.labels or {}),
                        }
                    ),
                    params=_json_dumps(dict(stage_spec.params or {})),
                    shard_mode=stage_spec.shard.mode,
                    requires=_json_dumps(dict(stage_spec.requires or {})),
                    state="pending",
                    timeout_s=float(stage_spec.timeout_s),
                )
            )
        await self._emit_task_created(
            task_id=task_id,
            name=spec.name,
            labels=labels,
            source=source,
            priority=int(spec.priority),
        )
        # pending → planning / running 并激活首个 Stage
        task = await self._repo(TestTask).get(task_id)
        assert task is not None
        await self._start_first_stage(task)
        return task_id

    async def cancel(self, task_id: str, reason: str = "") -> bool:
        """请求取消任务；已终结或不存在返回 False。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None or task.state in TASK_FINISHED:
            return False
        now = self._clock()
        note = reason or "cancel requested"
        if task.state != "canceling":
            await self._transition_task(task, "canceling", reason=note)
        has_inflight = False
        for job in await self._repo(TestJob).list(where=TestJob.task_id == task_id):
            if job.state == "queued":
                await self._cancel_queued_job(job, reason=note)
            elif job.state in ("leased", "running"):
                self._scheduler.send_cancel(
                    job.node_id, job_id=job.id, attempt=job.attempt, reason=note
                )
                has_inflight = True
            elif job.state == "lost":
                has_inflight = True  # 重派 / 超时路径收敛（届时按取消口径收尾）
        if not has_inflight:
            await self._finish_cancel(task, now=now)
        return True

    async def rerun_failed(self, task_id: str) -> str:
        """重建失败 Job（同一 job 行 attempt+1，failed→queued），返回同一 task_id。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None:
            raise TaskNotFound(f"任务不存在：{task_id}")
        if task.state != "failed":
            raise InvalidTransition(f"只有 failed 任务可重跑，当前状态 {task.state!r}")
        failed_jobs = [
            job
            for job in await self._repo(TestJob).list(where=TestJob.task_id == task_id)
            if job.state == "failed"
        ]
        if not failed_jobs:
            return task_id  # 无失败 Job：原样返回（no-op）
        now = self._clock()
        await self._transition_task(task, "running", reason="rerun_failed")
        for job in failed_jobs:
            await self._update_job(
                job,
                "queued",
                reason="rerun_failed",
                attempt=job.attempt + 1,
                node_id="",
                lease_ts=0.0,
                finished_ts=0.0,
                error="",
            )
            self._enqueue(job, task, created_ts=now)
        return task_id

    async def set_priority(self, task_id: str, priority: int) -> bool:
        """调整任务优先级；不存在或已终结返回 False。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None or task.state in TASK_FINISHED:
            return False
        await self._repo(TestTask).update(
            task.id, {"priority": int(priority), "updated_ts": self._clock()}
        )
        self._scheduler.reprioritize(task_id, int(priority))
        return True

    # ============================================================== 状态机编排
    def _repo(self, model: Any) -> Any:
        """按模型归属取仓储。

        显式 ``owner=``（= 模型物理归属，db 的显式授权口）：服务方法经**调用方**
        fiber 执行（如 api 附属服务），调用方身份与模型归属可能不同名；显式归属
        仍受「owner 必须等于模型物理归属」校验。
        """
        return self.ctx.db.repo(model, owner=MODEL_OWNER)

    def _enqueue(
        self,
        job: TestJob,
        task: TestTask,
        *,
        created_ts: float,
        avoid_node: str = "",
    ) -> None:
        """job 入内存就绪队列（project 配额轴取任务 labels）。"""
        self._scheduler.enqueue(
            job.id,
            task.id,
            priority=task.priority,
            created_ts=created_ts,
            project=project_of(_decode_dict(task.labels)),
            avoid_node=avoid_node,
        )

    async def _transition_task(
        self, task: TestTask, to: str, *, reason: str, error: str | None = None
    ) -> None:
        """任务状态迁移：写行 + task_history + emit test/task-state（§5.1）。"""
        now = self._clock()
        from_state = task.state
        transition("task", from_state, to, reason=reason)
        values: dict[str, object] = {"state": to, "updated_ts": now}
        if to in TASK_FINISHED:
            values["finished_ts"] = now
            values["error"] = error if error is not None else (reason if to in ("failed", "error") else "")
        else:
            values["finished_ts"] = 0.0
        await self._repo(TestTask).update(task.id, values)
        task.state = to
        await self._append_history(task.id, from_state, to, reason, now)
        await self._emit_task_state(
            task_id=task.id, from_state=from_state, to=to, reason=reason, at=now
        )

    async def _append_history(
        self, task_id: str, from_state: str, to_state: str, reason: str, now: float
    ) -> None:
        """写 task_history 并按 history_size 裁剪（DESIGN §13）。"""
        repo = self._repo(TestTaskHistory)
        await repo.add(
            TestTaskHistory(
                **history_row(task_id, from_state, to_state, reason, now, _new_id("h", now))
            )
        )
        rows = await repo.list(
            where=TestTaskHistory.task_id == task_id,
            order_by=[TestTaskHistory.ts, TestTaskHistory.id],
        )
        excess = len(rows) - self.config.history_size
        for row in rows[: max(0, excess)]:
            await repo.delete(row.id)

    async def _update_job(
        self, job: TestJob, to: str, *, reason: str = "", **values: object
    ) -> None:
        """job 状态迁移（registry.transition 校验）+ 写行。"""
        transition("job", job.state, to, reason=reason)
        data: dict[str, object] = {"state": to, **values}
        await self._repo(TestJob).update(job.id, data)
        job.state = to

    async def _start_first_stage(self, task: TestTask) -> None:
        """激活首个 Stage：pending → planning / running（plan 型先置 planning）。"""
        stages = await self._stage_rows(task.id)
        if not stages:
            await self._transition_task(
                task, "error", reason="task has no stages", error="task has no stages"
            )
            return
        first = stages[0]
        to = "planning" if first.shard_mode == "plan" else "running"
        await self._transition_task(task, to, reason="submit")
        await self._start_stage(task, first)

    async def _resume_stage(self, task: TestTask) -> None:
        """恢复半途任务：当前 Stage 无任何 job 时补建（崩溃在建 job 前）。"""
        stages = await self._stage_rows(task.id)
        current = next((s for s in stages if s.name == task.current_stage), None)
        if current is None:
            current = stages[0] if stages else None
        if current is None:
            await self._transition_task(
                task, "error", reason="task has no stages", error="task has no stages"
            )
            return
        await self._start_stage(task, current)

    async def _start_stage(self, task: TestTask, stage: TestStage) -> None:
        """激活 Stage：建 plan / run job 入队（§5.1 / §9.3）。"""
        now = self._clock()
        await self._repo(TestStage).update(stage.id, {"state": "running"})
        await self._repo(TestTask).update(
            task.id, {"current_stage": stage.name, "updated_ts": now}
        )
        task.current_stage = stage.name
        if stage.shard_mode == "plan":
            await self._create_job(task, stage, shard={"plan": True})
        else:
            await self._create_job(task, stage, shard=None)

    async def _create_job(
        self, task: TestTask, stage: TestStage, *, shard: dict[str, Any] | None
    ) -> TestJob:
        """建 job 行（attempt=1）并入就绪队列。"""
        now = self._clock()
        job = TestJob(
            id=_new_id("j", now),
            stage_id=stage.id,
            task_id=task.id,
            node_id="",
            attempt=1,
            state="queued",
            shard=_json_dumps(shard) if shard is not None else "null",
            lease_ts=0.0,
            started_ts=0.0,
            finished_ts=0.0,
            error="",
            result_seq=0,
        )
        await self._repo(TestJob).add(job)
        self._enqueue(job, task, created_ts=now)
        return job

    async def _stage_rows(self, task_id: str) -> list[TestStage]:
        """任务的全部 Stage（seq 升序）。"""
        return await self._repo(TestStage).list(
            where=TestStage.task_id == task_id, order_by=TestStage.seq
        )

    async def _job_rows(self, task_id: str) -> list[TestJob]:
        """任务的全部 Job 行。"""
        return await self._repo(TestJob).list(where=TestJob.task_id == task_id)

    # ============================================================== 调度循环
    async def _dispatch_loop(self) -> None:
        """派发循环：按窗口 / 配额出队派发（异常隔离，不退出循环）。"""
        while True:
            await asyncio.sleep(_DISPATCH_INTERVAL)
            try:
                await self._dispatch_tick()
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.error("调度循环出错：%s", error)

    async def _dispatch_tick(self) -> None:
        """一轮派发：窗口内逐个选节点派发，直到队空 / 满窗 / 无候选节点。"""
        if self._scheduler.queue_depth() == 0:
            return
        cfg = self.config
        inflight = await self._scheduler.inflight()
        skip: set[str] = set()
        while inflight.total < cfg.max_inflight_jobs:
            entry = self._scheduler.peek(
                inflight=inflight, project_quota=cfg.project_quota, skip=skip
            )
            if entry is None:
                return
            job = await self._repo(TestJob).get(entry.job_id)
            if job is None or job.state != "queued":
                self._scheduler.drop(entry.job_id)
                continue
            task = await self._repo(TestTask).get(job.task_id)
            if task is None:
                self._scheduler.drop(entry.job_id)
                continue
            if task.state == "canceling" or task.state in TASK_FINISHED:
                await self._cancel_queued_job(job, reason="task no longer active")
                continue
            if task.state not in ("planning", "running"):
                self._scheduler.drop(entry.job_id)
                continue
            stage = await self._repo(TestStage).get(job.stage_id)
            if stage is None:
                self._scheduler.drop(entry.job_id)
                continue
            executor = _executor_of(stage)
            node_id = self._scheduler.pick_node(
                executor, inflight=inflight, avoid=entry.avoid_node
            )
            if node_id is None:
                return  # 无候选节点：保留队首，下一轮再试
            now = self._clock()
            timeout = stage.timeout_s if stage.timeout_s > 0 else cfg.job_timeout
            shard = _decode_json(job.shard, None)
            descriptor = JobDescriptor(
                proto=TEST_PROTO,
                job_id=job.id,
                task_id=task.id,
                stage_name=stage.name,
                attempt=job.attempt,
                executor=executor,
                params=_job_params(task, stage, shard),
                shard=_descriptor_shard(shard),
                artifact_dir=f"data/plugins/test_core/artifacts/{task.id}/{job.id}/a{job.attempt}",
                deadline_ts=now + timeout,
            )
            # 派发即 job：queued → leased（不发事件，§5.2）
            await self._update_job(
                job,
                "leased",
                reason="dispatch",
                node_id=node_id,
                lease_ts=now,
            )
            self._scheduler.take(job.id)
            self._scheduler.send_job(node_id, descriptor, ttl=timeout)
            inflight.note(node_id, project_of(_decode_dict(task.labels)))

    async def _cancel_queued_job(self, job: TestJob, *, reason: str) -> None:
        """取消排队中的 job（queued → canceled）：出队 + 收尾 + 屏障评估。"""
        self._scheduler.drop(job.id)
        await self._finish_job(job, "canceled", summary={}, error=reason or "canceled")

    # ============================================================== 租约扫描
    async def _sweep_loop(self) -> None:
        """租约扫描循环：周期 max(1, min(5, lease_timeout/4))（§10）。"""
        period = max(1.0, min(5.0, self.config.lease_timeout / 4.0))
        while True:
            await asyncio.sleep(period)
            try:
                await self._sweep_once()
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.error("租约扫描出错：%s", error)

    async def _sweep_once(self) -> None:
        """一轮扫描：节点失联提前 lost → 租约超时 → 任务总超时 → 仪表刷新。"""
        now = self._clock()
        seen: set[str] = set()
        while self._node_events:
            node_id = self._node_events.popleft()
            if node_id in seen:
                continue
            seen.add(node_id)
            for job in await self._scheduler.jobs_on_node(node_id):
                self._scheduler.count_lease_timeout()
                await self._handle_lost(job, reason=f"node {node_id} offline/lost")
        for job in await self._scheduler.inflight_jobs():
            if self._scheduler.lease_expired(
                job, now=now, lease_timeout=self.config.lease_timeout
            ):
                self._scheduler.count_lease_timeout()
                await self._handle_lost(job, reason="lease timeout")
        await self._fail_timeout_tasks(now)
        await self._refresh_gauges()

    async def _fail_timeout_tasks(self, now: float) -> None:
        """任务总超时：running → failed（error="task timeout"），在途 job 发取消。"""
        for task in await self._repo(TestTask).list(where=TestTask.state == "running"):
            timeout = self._scheduler.task_timeout_of(task)
            if timeout <= 0 or (now - task.created_ts) <= timeout:
                continue
            # 先落终态（屏障对已终结任务幂等），再处置在途 job
            await self._transition_task(
                task, "failed", reason="task timeout", error="task timeout"
            )
            for job in await self._job_rows(task.id):
                if job.state == "queued":
                    await self._cancel_queued_job(job, reason="task timeout")
                elif job.state in ("leased", "running"):
                    self._scheduler.send_cancel(
                        job.node_id,
                        job_id=job.id,
                        attempt=job.attempt,
                        reason="task timeout",
                    )
            counts = await self._aggregate_counts(task.id)
            await self._emit_task_finished(
                task_id=task.id,
                state="failed",
                counts=counts,
                duration_s=max(0.0, now - task.created_ts),
            )

    async def _handle_lost(self, job: TestJob, *, reason: str) -> None:
        """lost 路径：未超 max_attempts → attempt+1 重新入队（换节点优先）；否则 failed。"""
        now = self._clock()
        if job.state not in ("leased", "running"):
            return
        await self._update_job(job, "lost", reason=reason, error=reason)
        task = await self._repo(TestTask).get(job.task_id)
        if task is not None and task.state in TASK_FINISHED:
            # 任务已终结（超时/取消收尾后残留）：不再重派，直接判失败收口
            await self._finish_job(job, "failed", summary={}, error="task already finished", now=now)
            return
        if job.attempt < self.config.max_attempts:
            await self._update_job(
                job,
                "queued",
                reason=reason,
                attempt=job.attempt + 1,
                node_id="",
                lease_ts=0.0,
                error=reason,
            )
            self._scheduler.count_retry()
            if task is not None:
                self._enqueue(job, task, created_ts=now, avoid_node=job.node_id)
            return
        await self._finish_job(job, "failed", summary={}, error=reason, now=now)

    # ============================================================== 结果接收
    async def _on_job_result(self, payload: object, _meta: object) -> None:
        """``test.job.result`` 收件（comm 处理器，异步；异常隔离不外抛）。"""
        try:
            await self._handle_job_result(payload)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("处理 test.job.result 失败：%s", error)

    async def _handle_job_result(self, raw: object) -> None:
        """结果主路径：幂等 → 迟到 → proto 握手 → transform 链 → 落库聚合。"""
        if not isinstance(raw, Mapping):
            self._warn("test.job.result 载荷不是对象，忽略")
            return
        job_id = str(raw.get("job_id") or "")
        try:
            attempt = int(raw.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if not job_id or attempt < 1:
            self._warn(f"test.job.result 缺 job_id/attempt，忽略：{raw!r}")
            return
        result_id = f"{job_id}:{attempt}"
        result_repo = self._repo(TestJobResult)
        if await result_repo.get(result_id) is not None:
            self._results_duplicate_total += 1
            self._warn(f"重复的结果上报（{result_id}），只计数不聚合")
            return
        job = await self._repo(TestJob).get(job_id)
        if job is None:
            self._warn(f"结果上报指向未知 job（{result_id}），忽略")
            return
        now = self._clock()
        proto = raw.get("proto")
        # 迟到结果：attempt 小于当前 —— 仍存结果行，不改 job 状态、不聚合（§5.2）
        if attempt < job.attempt:
            await result_repo.add(
                TestJobResult(
                    id=result_id,
                    job_id=job_id,
                    task_id=job.task_id,
                    attempt=attempt,
                    status=str(raw.get("status") or "failed"),
                    summary=_json_dumps(_decode_dict(raw.get("summary"))),
                    started_ts=float(raw.get("started_ts") or 0.0),
                    finished_ts=float(raw.get("finished_ts") or 0.0),
                    error=str(raw.get("error") or ""),
                    received_ts=now,
                )
            )
            self._late_results_total += 1
            self._warn(f"迟到的结果上报（{result_id}），落库但不聚合")
            return
        # proto 握手：不认识的版本记 error、不重试（§8.3）
        if proto != TEST_PROTO:
            error = f"proto mismatch: got {proto!r}, support {TEST_PROTO}"
            await result_repo.add(
                TestJobResult(
                    id=result_id,
                    job_id=job_id,
                    task_id=job.task_id,
                    attempt=attempt,
                    status="failed",
                    summary=_json_dumps({}),
                    started_ts=float(raw.get("started_ts") or 0.0),
                    finished_ts=float(raw.get("finished_ts") or 0.0),
                    error=error,
                    received_ts=now,
                )
            )
            if job.state in ("leased", "running"):
                await self._finish_job(job, "failed", summary={}, error=error, now=now)
            return

        result = _parse_job_result(raw)
        # result.transform：落库前归一化（可改 summary / cases）
        hook_ctx = HookCtx(
            chain=CHAIN_RESULT_TRANSFORM,
            result=result,
            meta={"job_id": job_id, "attempt": attempt, "task_id": job.task_id},
        )
        hook_ctx = await self._hooks.run_result_transform(hook_ctx)
        result = hook_ctx.result if hook_ctx.result is not None else result

        await result_repo.add(
            TestJobResult(
                id=result_id,
                job_id=job_id,
                task_id=job.task_id,
                attempt=attempt,
                status=result.status,
                summary=_json_dumps(dict(result.summary)),
                started_ts=result.started_ts,
                finished_ts=result.finished_ts,
                error=result.error,
                received_ts=now,
            )
        )
        if job.state not in ("leased", "running"):
            # 竞态（已终结 / 已 lost 后补报）：落库不聚合，同迟到口径
            self._late_results_total += 1
            self._warn(f"结果上报竞态（{result_id}，job={job.state}），落库但不聚合")
            return

        limit = self.config.case_results_limit
        cases = list(result.cases)
        truncated = len(cases) > limit
        for index, case in enumerate(cases[: max(0, limit)]):
            await self._repo(TestCaseResult).add(
                TestCaseResult(
                    id=f"{result_id}:{index}",
                    job_id=job_id,
                    task_id=job.task_id,
                    attempt=attempt,
                    case_id=case.case_id,
                    status=case.status,
                    duration_s=case.duration_s,
                    message=case.message,
                    detail_ref=case.detail_ref,
                )
            )
        stored_artifacts: list[tuple[TestArtifact, ArtifactRef]] = []
        art_repo = self._repo(TestArtifact)
        for ref in result.artifacts:
            artifact_id = ref.artifact_id or _new_id("a", now)
            if await art_repo.get(artifact_id) is not None:
                self._warn(f"产物 id 重复（{artifact_id}），跳过登记")
                continue
            row = TestArtifact(
                id=artifact_id,
                job_id=job_id,
                task_id=job.task_id,
                kind=ref.kind,
                name=ref.name,
                uri=ref.uri,
                size=ref.size,
                sha256=ref.sha256,
                state="local",
                created_ts=now,
            )
            await art_repo.add(row)
            stored_artifacts.append((row, ref))

        to = _JOB_RESULT_STATES.get(result.status, "failed")
        await self._update_job(
            job,
            to,
            reason=result.error or result.status,
            finished_ts=result.finished_ts or now,
            started_ts=result.started_ts or job.started_ts,
            error=result.error,
            result_seq=job.result_seq + 1,
        )
        await self._emit_job_finished(
            task_id=job.task_id,
            job_id=job.id,
            attempt=attempt,
            status=to,
            node_id=job.node_id,
            summary=dict(result.summary),
        )
        if _is_plan_job(job):
            if result.status == "succeeded":
                await self._expand_plan(job, result)
            # 分片清单就绪（或 plan 失败）：planning → running（§5.1）
            await self._leave_planning(job.task_id, reason="shard plan finished")
        failed = [
            {"case_id": case.case_id, "message": case.message, "detail_ref": case.detail_ref}
            for case in cases
            if case.status in ("failed", "error")
        ]
        if failed:
            await self._emit_case_finished(
                task_id=job.task_id,
                job_id=job.id,
                attempt=attempt,
                failed=failed[:_CASE_EVENT_LIMIT],
                truncated=truncated or len(failed) > _CASE_EVENT_LIMIT,
            )
        for row, ref in stored_artifacts:
            await self._emit_artifact_ready(
                artifact_id=row.id,
                task_id=job.task_id,
                job_id=job.id,
                kind=row.kind,
                uri=row.uri,
                size=row.size,
                sha256=row.sha256,
            )
            await self._run_artifact_sink(ref)
        await self._evaluate_barrier(job.task_id)

    async def _leave_planning(self, task_id: str, *, reason: str) -> None:
        """planning → running（分片清单就绪 / plan 收尾，§5.1）；已离开 planning 则不动。"""
        task = await self._repo(TestTask).get(task_id)
        if task is not None and task.state == "planning":
            await self._transition_task(task, "running", reason=reason)

    async def _expand_plan(self, job: TestJob, result: JobResult) -> None:
        """plan 结果 → 分片 job（§9.3）：shards 空/缺失建 1 个无分片 job。"""
        task = await self._repo(TestTask).get(job.task_id)
        stage = await self._repo(TestStage).get(job.stage_id)
        if task is None or stage is None:
            return
        raw_shards = result.summary.get("shards")
        shards = [item for item in (raw_shards or []) if isinstance(item, dict)]
        if not isinstance(raw_shards, list) or not shards:
            await self._create_job(task, stage, shard=None)
            return
        total = len(shards)
        for index, item in enumerate(shards):
            unit_refs = item.get("unit_refs")
            shard: dict[str, Any] = {
                "index": index,
                "total": total,
                "unit_refs": list(unit_refs) if isinstance(unit_refs, (list, tuple)) else [],
            }
            override = item.get("params")
            if isinstance(override, dict) and override:
                shard["params"] = dict(override)  # 分片级参数覆盖（派发时并入）
            await self._create_job(task, stage, shard=shard)

    async def _on_job_progress(self, payload: object, _meta: object) -> None:
        """``test.job.progress`` 收件（fire-and-forget，丢了无害）。"""
        try:
            await self._handle_job_progress(payload)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("处理 test.job.progress 失败：%s", error)

    async def _handle_job_progress(self, raw: object) -> None:
        """进度：首次记 started_ts、leased→running（§5.2）；note 无处存仅日志。"""
        if not isinstance(raw, Mapping):
            return
        job_id = str(raw.get("job_id") or "")
        job = await self._repo(TestJob).get(job_id)
        if job is None or job.state not in ("leased", "running"):
            return
        try:
            attempt = int(raw.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        if attempt and attempt != job.attempt:
            return  # 过期 attempt 的进度
        values: dict[str, object] = {}
        if job.started_ts <= 0:
            values["started_ts"] = self._clock()
        if job.state == "leased":
            values["state"] = "running"
        if values:
            await self._repo(TestJob).update(job.id, values)
        note = raw.get("note")
        if note:
            self.ctx.logger.info("job %s 进度：%s", job_id, note)

    def _on_node_status(self, payload: object) -> None:
        """``heartbeat/node-status``（emit）：同步监听器只投递 deque（§10）。"""
        if not isinstance(payload, Mapping):
            return
        node_id = str(payload.get("node_id") or "")
        to = str(payload.get("to") or "")
        if node_id and to in ("offline", "lost"):
            self._node_events.append(node_id)

    # ============================================================== 屏障与收尾
    async def _finish_job(
        self,
        job: TestJob,
        to: str,
        *,
        summary: Mapping[str, Any],
        error: str,
        now: float | None = None,
    ) -> None:
        """job 终结：写行 + emit test/job-finished + Stage 屏障评估。"""
        at = self._clock() if now is None else now
        await self._update_job(
            job,
            to,
            reason=error or to,
            finished_ts=at,
            error=error,
        )
        await self._emit_job_finished(
            task_id=job.task_id,
            job_id=job.id,
            attempt=job.attempt,
            status=to,
            node_id=job.node_id,
            summary=dict(summary),
        )
        await self._evaluate_barrier(job.task_id)

    async def _evaluate_barrier(self, task_id: str) -> None:
        """Stage 屏障（§5.2）：Stage 内全部 job 终结 → 判定并推进 / 收尾。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None or task.state in TASK_FINISHED:
            return
        jobs = await self._job_rows(task_id)
        if any(job.state not in JOB_FINISHED for job in jobs):
            return
        if task.state == "canceling":
            await self._finish_cancel(task)
            return
        # 安全兜底：plan 收尾事件丢失（恢复 / 竞态）时补走 planning → running
        await self._leave_planning(task_id, reason="shard plan finished")
        task = await self._repo(TestTask).get(task_id)
        if task is None or task.state in TASK_FINISHED:
            return
        stages = await self._stage_rows(task_id)
        current = next((s for s in stages if s.name == task.current_stage), None)
        if current is None:
            return
        stage_jobs = [job for job in jobs if job.stage_id == current.id]
        succeeded = bool(stage_jobs) and all(job.state == "succeeded" for job in stage_jobs)
        await self._repo(TestStage).update(
            current.id, {"state": "succeeded" if succeeded else "failed"}
        )
        if not succeeded and task.failure_policy == "fail_fast":
            await self._finish_task(task, "failed", reason=f"stage {current.name} failed")
            return
        later = [stage for stage in stages if stage.seq > current.seq]
        if later:
            # "continue"：无论成败推进下一 Stage（running 态不变，无迁移事件）
            await self._start_stage(task, later[0])
            return
        all_succeeded = all(
            stage.state == "succeeded" for stage in stages if stage.id != current.id
        ) and succeeded
        await self._finish_task(
            task, "succeeded" if all_succeeded else "failed", reason="all stages finished"
        )

    async def _finish_task(self, task: TestTask, to: str, *, reason: str) -> None:
        """任务终结：状态迁移 + 聚合计数 + emit test/task-finished（§11.1）。"""
        now = self._clock()
        counts = await self._aggregate_counts(task.id)
        await self._transition_task(task, to, reason=reason)
        await self._emit_task_finished(
            task_id=task.id,
            state=to,
            counts=counts,
            duration_s=max(0.0, now - task.created_ts),
        )

    async def _finish_cancel(self, task: TestTask, *, now: float | None = None) -> None:
        """取消收口：canceling → canceled + emit test/task-finished（幂等）。

        纯排队任务的收口可能被 ``_cancel_queued_job`` 的屏障评估与 ``cancel()``
        的兜底各触发一次；已离开 canceling（收口完成 / 已终结）即静默返回，
        保证 task_history 与 task-finished 事件只记一次。
        """
        fresh = await self._repo(TestTask).get(task.id)
        if fresh is None or fresh.state != "canceling":
            return
        at = self._clock() if now is None else now
        counts = await self._aggregate_counts(fresh.id)
        await self._transition_task(fresh, "canceled", reason="canceled")
        await self._emit_task_finished(
            task_id=fresh.id,
            state="canceled",
            counts=counts,
            duration_s=max(0.0, at - fresh.created_ts),
        )

    async def _aggregate_counts(self, task_id: str) -> dict[str, float]:
        """聚合计数：每 job 最新 attempt 结果的 summary 数值键求和。"""
        counts: dict[str, float] = {}
        for job in await self._job_rows(task_id):
            row = await self._repo(TestJobResult).get(f"{job.id}:{job.attempt}")
            if row is None:
                continue
            for key, value in _decode_dict(row.summary).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    counts[key] = counts.get(key, 0.0) + float(value)
        return {
            key: int(value) if float(value).is_integer() else value
            for key, value in counts.items()
        }

    # ============================================================== 查询门面
    async def get_task(self, task_id: str) -> Mapping[str, Any] | None:
        """任务详情（含 stages 与 extensions 收集槽）；不存在返回 None。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None:
            return None
        detail: dict[str, Any] = self._task_dict(task)
        detail["stages"] = [self._stage_dict(s) for s in await self._stage_rows(task_id)]
        # extensions 收集槽（同 heartbeat collect-node 模式，§11.3）
        detail["extensions"] = {}
        detail["extension_errors"] = []
        try:
            await self.ctx.parallel(EVENT_COLLECT_TASK, detail)
        except EventDispatchError as exc:
            detail["extension_errors"] = [
                f"{type(error).__name__}: {error}" for error in exc.errors
            ]
        return detail

    async def list_tasks(
        self,
        *,
        state: str | None = None,
        labels: Mapping[str, str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        """任务列表（state 精确过滤、labels 子集匹配、分页）。"""
        where = TestTask.state == state if state else None
        rows = await self._repo(TestTask).list(where=where, order_by=TestTask.created_ts)
        wanted = dict(labels or {})
        if wanted:
            rows = [
                row
                for row in rows
                if all(_decode_dict(row.labels).get(k) == v for k, v in wanted.items())
            ]
        start = max(0, offset)
        end = start + max(0, limit)
        return [self._task_dict(row) for row in rows[start:end]]

    async def list_jobs(self, task_id: str) -> list[Mapping[str, Any]]:
        """任务的 Job 列表（含各 attempt 的结果历史）。"""
        result_repo = self._repo(TestJobResult)
        out: list[Mapping[str, Any]] = []
        for job in await self._job_rows(task_id):
            entry = self._job_dict(job)
            attempts = await result_repo.list(
                where=TestJobResult.job_id == job.id, order_by=TestJobResult.attempt
            )
            entry["results"] = [self._result_dict(row) for row in attempts]
            out.append(entry)
        return out

    async def list_task_history(
        self, task_id: str, *, limit: int = 200
    ) -> list[Mapping[str, Any]]:
        """任务状态迁移时间线（按 ts/id 排序，最多 limit 条）。"""
        rows = await self._repo(TestTaskHistory).list(
            where=TestTaskHistory.task_id == task_id,
            order_by=[TestTaskHistory.ts, TestTaskHistory.id],
        )
        entries: list[Mapping[str, Any]] = [
            {
                "id": row.id,
                "task_id": row.task_id,
                "from_state": row.from_state,
                "to_state": row.to_state,
                "reason": row.reason,
                "ts": row.ts,
            }
            for row in rows
        ]
        return entries[: max(0, int(limit))]

    async def get_result(self, task_id: str) -> Mapping[str, Any] | None:
        """聚合结果：summary 数值键求和 + jobs 明细 + cases / artifacts 计数。"""
        task = await self._repo(TestTask).get(task_id)
        if task is None:
            return None
        jobs = await self._job_rows(task_id)
        counts: dict[str, float] = {}
        details: list[dict[str, Any]] = []
        latest: set[str] = set()
        result_repo = self._repo(TestJobResult)
        for job in jobs:
            row = await result_repo.get(f"{job.id}:{job.attempt}")
            latest.add(f"{job.id}:{job.attempt}")
            summary = _decode_dict(row.summary) if row is not None else {}
            for key, value in summary.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    counts[key] = counts.get(key, 0.0) + float(value)
            details.append(
                {
                    "job_id": job.id,
                    "attempt": job.attempt,
                    "status": row.status if row is not None else job.state,
                    "node_id": job.node_id,
                    "summary": summary,
                }
            )
        case_counts: dict[str, int] = {}
        for row in await self._repo(TestCaseResult).list(
            where=TestCaseResult.task_id == task_id
        ):
            if f"{row.job_id}:{row.attempt}" in latest:
                case_counts[row.status] = case_counts.get(row.status, 0) + 1
        artifacts = await self._repo(TestArtifact).count(where=TestArtifact.task_id == task_id)
        finished = task.finished_ts if task.finished_ts > 0 else self._clock()
        return {
            "task_id": task_id,
            "state": task.state,
            "created_ts": task.created_ts,
            "finished_ts": task.finished_ts,
            "duration_s": max(0.0, finished - task.created_ts),
            "counts": {
                key: int(value) if float(value).is_integer() else value
                for key, value in counts.items()
            },
            "jobs": details,
            "cases": case_counts,
            "artifacts": artifacts,
        }

    async def list_case_results(
        self,
        *,
        task_id: str = "",
        job_id: str = "",
        status: str = "",
        limit: int = 200,
    ) -> list[Mapping[str, Any]]:
        """用例结果查询（task_id / job_id / status 过滤）。"""
        conditions = []
        if task_id:
            conditions.append(TestCaseResult.task_id == task_id)
        if job_id:
            conditions.append(TestCaseResult.job_id == job_id)
        if status:
            conditions.append(TestCaseResult.status == status)
        where = None
        for condition in conditions:
            where = condition if where is None else (where & condition)
        rows = await self._repo(TestCaseResult).list(where=where, limit=max(0, limit))
        return [
            {
                "id": row.id,
                "job_id": row.job_id,
                "task_id": row.task_id,
                "attempt": row.attempt,
                "case_id": row.case_id,
                "status": row.status,
                "duration_s": row.duration_s,
                "message": row.message,
                "detail_ref": row.detail_ref,
            }
            for row in rows
        ]

    async def list_artifacts(
        self, *, task_id: str = "", state: str = "local"
    ) -> list[Mapping[str, Any]]:
        """产物列表（state 过滤，默认本地产物）。"""
        conditions = []
        if task_id:
            conditions.append(TestArtifact.task_id == task_id)
        if state:
            conditions.append(TestArtifact.state == state)
        where = None
        for condition in conditions:
            where = condition if where is None else (where & condition)
        rows = await self._repo(TestArtifact).list(where=where, order_by=TestArtifact.created_ts)
        return [
            {
                "artifact_id": row.id,
                "job_id": row.job_id,
                "task_id": row.task_id,
                "kind": row.kind,
                "name": row.name,
                "uri": row.uri,
                "size": row.size,
                "sha256": row.sha256,
                "state": row.state,
                "created_ts": row.created_ts,
            }
            for row in rows
        ]

    async def mark_artifact(
        self, artifact_id: str, *, uri: str | None = None, state: str | None = None
    ) -> bool:
        """产物状态 / URI 回填（artifact_oss / artifact_gc 专用写口）；不存在返回 False。"""
        repo = self._repo(TestArtifact)
        if await repo.get(artifact_id) is None:
            return False
        values: dict[str, object] = {}
        if uri is not None:
            values["uri"] = uri
        if state is not None:
            values["state"] = state
        if values:
            await repo.update(artifact_id, values)
        return True

    # ============================================================== 行 → dict
    def _task_dict(self, row: TestTask) -> dict[str, Any]:
        return {
            "task_id": row.id,
            "idempotency_key": row.idempotency_key,
            "source": row.source,
            "name": row.name,
            "created_by": row.created_by,
            "priority": row.priority,
            "labels": _decode_dict(row.labels),
            "params": _public_params(_decode_dict(row.params)),
            "state": row.state,
            "current_stage": row.current_stage,
            "failure_policy": row.failure_policy,
            "error": row.error,
            "created_ts": row.created_ts,
            "updated_ts": row.updated_ts,
            "finished_ts": row.finished_ts,
        }

    def _stage_dict(self, row: TestStage) -> dict[str, Any]:
        return {
            "id": row.id,
            "task_id": row.task_id,
            "seq": row.seq,
            "name": row.name,
            "kind": row.kind,
            "executor": _decode_dict(row.executor),
            "params": _decode_dict(row.params),
            "shard_mode": row.shard_mode,
            "requires": _decode_dict(row.requires),
            "state": row.state,
            "timeout_s": row.timeout_s,
        }

    def _job_dict(self, row: TestJob) -> dict[str, Any]:
        return {
            "job_id": row.id,
            "stage_id": row.stage_id,
            "task_id": row.task_id,
            "node_id": row.node_id,
            "attempt": row.attempt,
            "state": row.state,
            "shard": _decode_json(row.shard, None),
            "lease_ts": row.lease_ts,
            "started_ts": row.started_ts,
            "finished_ts": row.finished_ts,
            "error": row.error,
        }

    def _result_dict(self, row: TestJobResult) -> dict[str, Any]:
        return {
            "id": row.id,
            "job_id": row.job_id,
            "attempt": row.attempt,
            "status": row.status,
            "summary": _decode_dict(row.summary),
            "started_ts": row.started_ts,
            "finished_ts": row.finished_ts,
            "error": row.error,
            "received_ts": row.received_ts,
        }

    # ============================================================== HookChain
    def register_hook(
        self,
        chain: str,
        fn: Callable[..., Any],
        *,
        phase: str,
        order: int,
        name: str,
        on_error: str = "fail",
    ) -> Callable[[], None]:
        """登记拦截器，返回注销函数（注销绑定调用方 fiber 生命周期）。"""
        strategies = {"fail": OnError.FAIL, "skip": OnError.SKIP}
        if on_error not in strategies:
            raise ValueError(f"on_error 非法，收到 {on_error!r}（可选：fail / skip）")
        dispose = self._hooks.register(
            chain, fn, phase=phase, order=order, name=name, on_error=strategies[on_error]
        )
        # execute 的返回值即清理函数：随调用方 fiber 卸载自动注销
        self.ctx.effect(lambda: dispose, f"test_core.register_hook({name!r})")
        return dispose

    async def _run_artifact_sink(self, ref: ArtifactRef) -> None:
        """artifact.sink 旁路链：只读 ref，钩子错误不影响主流程（§11.2）。"""
        try:
            await self._hooks.run_artifact_sink(
                HookCtx(chain=CHAIN_ARTIFACT_SINK, ref=ref)
            )
        except Exception as error:  # noqa: BLE001
            self._warn(f"artifact.sink 钩子出错（已忽略）：{error}")

    # ============================================================== 事件派发
    async def _emit_task_created(
        self, *, task_id: str, name: str, labels: Mapping[str, str], source: str, priority: int
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_TASK_CREATED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "name": name,
                    "labels": dict(labels),
                    "source": source,
                    "priority": priority,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/task-created", exc)

    async def _emit_task_state(
        self, *, task_id: str, from_state: str, to: str, reason: str, at: float
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_TASK_STATE,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "from": from_state,
                    "to": to,
                    "reason": reason,
                    "at": at,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/task-state", exc)

    async def _emit_task_finished(
        self, *, task_id: str, state: str, counts: Mapping[str, Any], duration_s: float
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_TASK_FINISHED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "state": state,
                    "counts": dict(counts),
                    "duration_s": duration_s,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/task-finished", exc)

    async def _emit_job_finished(
        self,
        *,
        task_id: str,
        job_id: str,
        attempt: int,
        status: str,
        node_id: str,
        summary: Mapping[str, Any],
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_JOB_FINISHED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "status": status,
                    "node_id": node_id,
                    "summary": dict(summary),
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/job-finished", exc)

    async def _emit_case_finished(
        self,
        *,
        task_id: str,
        job_id: str,
        attempt: int,
        failed: Sequence[Mapping[str, str]],
        truncated: bool,
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_CASE_FINISHED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "failed": [dict(item) for item in failed],
                    "truncated": truncated,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/case-finished", exc)

    async def _emit_artifact_ready(
        self,
        *,
        artifact_id: str,
        task_id: str,
        job_id: str,
        kind: str,
        uri: str,
        size: int,
        sha256: str,
    ) -> None:
        try:
            await self.ctx.parallel(
                EVENT_ARTIFACT_READY,
                {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_id": artifact_id,
                    "task_id": task_id,
                    "job_id": job_id,
                    "kind": kind,
                    "uri": uri,
                    "size": size,
                    "sha256": sha256,
                },
            )
        except EventDispatchError as exc:
            self._log_dispatch_error("test/artifact-ready", exc)

    def _log_dispatch_error(self, event: str, exc: EventDispatchError) -> None:
        """派发失败不打断主流程，只记 warning（parallel 已隔离监听器异常）。"""
        self._warn(
            f"事件 {event} 派发有监听器出错："
            + "；".join(f"{type(error).__name__}: {error}" for error in exc.errors)
        )

    # ============================================================== 可观测性
    async def _refresh_gauges(self) -> None:
        """刷新 /healthz 仪表缓存（扫描循环定期调用）。"""
        self._gauge_inflight_jobs = len(await self._scheduler.inflight_jobs())
        self._gauge_running_tasks = await self._repo(TestTask).count(
            where=TestTask.state == "running"
        )

    def health_metrics(self) -> dict[str, object]:
        """/healthz 指标：队列 / 在途 / 运行任务与累计计数（只读）。"""
        return {
            **self._scheduler.metrics(),
            "inflight_jobs": self._gauge_inflight_jobs,
            "running_tasks": self._gauge_running_tasks,
            "results_duplicate_total": self._results_duplicate_total,
            "late_results_total": self._late_results_total,
            "hooks": self._hooks.stats(),
        }

    def _warn(self, message: str) -> None:
        self.ctx.logger.warning("%s", message)


# ============================================================== 载荷解析辅助
def _executor_of(stage: TestStage) -> ExecutorSelector:
    """stage.executor JSON → ExecutorSelector（坏行降级空选择器）。"""
    data = _decode_dict(stage.executor)
    labels = data.get("labels")
    return ExecutorSelector(
        id=str(data.get("id") or ""),
        kind=str(data.get("kind") or "executor"),
        labels=dict(labels) if isinstance(labels, dict) else {},
    )


def _job_params(
    task: TestTask, stage: TestStage, shard: object
) -> dict[str, Any]:
    """派发参数：任务级（注入各 Stage）→ Stage 参数 → 分片 params 覆盖。"""
    params: dict[str, Any] = _public_params(_decode_dict(task.params))
    params.update(_decode_dict(stage.params))
    if isinstance(shard, dict):
        override = shard.get("params")
        if isinstance(override, dict):
            params.update(override)
    return params


def _descriptor_shard(shard: object) -> dict[str, Any] | None:
    """JobDescriptor.shard：与 shard 列同形（分片 job 去掉内部 params 键）。"""
    if not isinstance(shard, dict):
        return None
    return {
        key: value for key, value in shard.items() if key != "params"
    }


def _is_plan_job(job: TestJob) -> bool:
    """是否 plan 型 job（shard 列存 {"plan": true}，§9.3）。"""
    shard = _decode_json(job.shard, None)
    return isinstance(shard, dict) and bool(shard.get("plan"))


def _parse_job_result(raw: Mapping[str, Any]) -> JobResult:
    """上行载荷 → JobResult（忽略未知字段、缺省给默认值，§3.2）。"""
    cases: list[CaseResult] = []
    for item in raw.get("cases") or []:
        if isinstance(item, Mapping):
            cases.append(
                CaseResult(
                    case_id=str(item.get("case_id") or ""),
                    status=str(item.get("status") or "failed"),  # type: ignore[arg-type]
                    duration_s=float(item.get("duration_s") or 0.0),
                    message=str(item.get("message") or ""),
                    detail_ref=str(item.get("detail_ref") or ""),
                )
            )
    artifacts: list[ArtifactRef] = []
    for item in raw.get("artifacts") or []:
        if isinstance(item, Mapping):
            artifacts.append(
                ArtifactRef(
                    artifact_id=str(item.get("artifact_id") or ""),
                    kind=str(item.get("kind") or "attachment"),  # type: ignore[arg-type]
                    name=str(item.get("name") or ""),
                    uri=str(item.get("uri") or ""),
                    size=int(item.get("size") or 0),
                    sha256=str(item.get("sha256") or ""),
                )
            )
    status = str(raw.get("status") or "failed")
    if status not in ("succeeded", "failed", "canceled", "timeout"):
        status = "failed"
    return JobResult(
        proto=int(raw.get("proto") or 0),
        job_id=str(raw.get("job_id") or ""),
        attempt=int(raw.get("attempt") or 0),
        status=status,  # type: ignore[arg-type]
        summary=_decode_dict(raw.get("summary")),
        cases=cases,
        artifacts=artifacts,
        started_ts=float(raw.get("started_ts") or 0.0),
        finished_ts=float(raw.get("finished_ts") or 0.0),
        error=str(raw.get("error") or ""),
    )


__all__: list[str] = ["TestCoreInterface", "TestCorePlugin"]
