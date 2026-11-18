"""test_core 调度器：就绪队列、派发窗口 / 配额、节点选择与租约扫描（DESIGN §9/§10）。

职责边界（与 :mod:`.plugin` 分工）：本模块只做**调度决策与 comm 收发**——
内存就绪队列（priority 降序、created_ts 升序）、全局 / 单节点派发窗口、
``labels["project"]`` 配额、节点选择（``ctx.nodes`` 能力匹配，缺席时经
``ctx.comm.nodes()`` 降级）、JobDescriptor 下行与取消下行、租约扫描的判定
查询；一切**状态机写库**与事件派发由 plugin 编排（状态机唯一写者）。

构造注入 db / comm / nodes 提供者、config、时钟与日志（时钟经 callable 注入，
测试可整体替换 ``plugin._clock``）；不 import 其他插件包，跨节点交互只走
``ctx.comm``。投递策略是**字面量 dict**（同 heartbeat ``_FIRE_AND_FORGET``
先例），不 import comm 包。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from common.test_contracts import (
    TOPIC_JOB,
    TOPIC_JOB_CANCEL,
    ExecutorSelector,
    JobDescriptor,
)

from .config import TestCoreConfig
from .models import TestJob, TestTask

#: 模型归属 = 插件 id（db 归属校验口径：生产取插件 id 解码、开发取 fiber 名，
#: 均为 "test-core"；slug 后物理表名 aetp_test_core_*）
MODEL_OWNER: str = "test-core"

#: TaskSpec.timeout_s 的持久化暂存键（task 表无 timeout 列的模型缺口）：
#: 写进 ``params`` JSON，读出与展示时剥离，不回流执行器参数
TASK_TIMEOUT_KEY: str = "__timeout_s__"

#: at-least-once 投递策略（comm DeliveryPolicy 字面量，不 import comm 包）：
#: 派发 / 取消必须送达，成功前无限重试（消息持久化、重启续传）
_AT_LEAST_ONCE: dict[str, int] = {"min_success": 1}

#: 节点失联 / 租约作废的在途状态（提前走 lost 路径的口径）
_IN_FLIGHT_STATES: tuple[str, ...] = ("leased", "running")


@dataclass
class QueueEntry:
    """内存就绪队列条目（job 行的调度元数据快照）。"""

    job_id: str
    task_id: str
    priority: int
    created_ts: float
    seq: int
    #: ``labels["project"]``（配额轴；空串 = 无项目）
    project: str = ""
    #: 上次执行节点（重派「换节点优先」，仅作排序偏好）
    avoid_node: str = ""


@dataclass
class InflightStats:
    """在途（leased/running）Job 的计数快照：全局 / 按节点 / 按项目。"""

    total: int = 0
    per_node: dict[str, int] = field(default_factory=dict)
    per_project: dict[str, int] = field(default_factory=dict)

    def note(self, node_id: str, project: str) -> None:
        """派发一个 job 后就地累加（同一轮窗口判断复用同一快照）。"""
        self.total += 1
        self.per_node[node_id] = self.per_node.get(node_id, 0) + 1
        if project:
            self.per_project[project] = self.per_project.get(project, 0) + 1


def project_of(labels: Mapping[str, str]) -> str:
    """配额轴：任务 ``labels["project"]``（缺席取空串 = 不占配额）。"""
    value = labels.get("project", "") if labels else ""
    return str(value or "")


class Scheduler:
    """就绪队列 + 派发窗口 / 配额 + 节点选择 + 租约扫描查询（编排调用方见 plugin）。"""

    def __init__(
        self,
        *,
        db: Any,
        comm: Any,
        config: TestCoreConfig,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None],
        nodes: Callable[[], Any | None] | None = None,
        self_name: Callable[[], str] | None = None,
    ) -> None:
        self._db = db
        self._comm = comm
        self._config = config
        self._clock = clock
        self._log = log
        self._nodes = nodes or (lambda: None)
        self._self_name = self_name or (lambda: "")
        self._queue: dict[str, QueueEntry] = {}
        self._seq: int = 0
        self._lease_timeouts_total: int = 0
        self._retries_total: int = 0
        self._no_nodes_warned: bool = False

    # ------------------------------------------------------------------ 队列
    def enqueue(
        self,
        job_id: str,
        task_id: str,
        *,
        priority: int,
        created_ts: float,
        project: str = "",
        avoid_node: str = "",
    ) -> None:
        """入队（同 job 重复入队覆盖元数据：attempt+1 重派取最新值）。"""
        self._seq += 1
        self._queue[job_id] = QueueEntry(
            job_id=job_id,
            task_id=task_id,
            priority=int(priority),
            created_ts=float(created_ts),
            seq=self._seq,
            project=project,
            avoid_node=avoid_node,
        )

    def drop(self, job_id: str) -> bool:
        """把 job 移出就绪队列（取消 / 行状态已变）；不在队列返回 False。"""
        return self._queue.pop(job_id, None) is not None

    def reprioritize(self, task_id: str, priority: int) -> None:
        """set_priority 后同步队列排序键（同任务的在队条目）。"""
        for entry in self._queue.values():
            if entry.task_id == task_id:
                entry.priority = int(priority)

    def queue_depth(self) -> int:
        """就绪队列深度（/healthz 指标）。"""
        return len(self._queue)

    def ordered(self) -> list[QueueEntry]:
        """出队序：priority 降序 → created_ts 升序 → 入队先后（同级 FIFO）。"""
        return sorted(
            self._queue.values(),
            key=lambda entry: (-entry.priority, entry.created_ts, entry.seq),
        )

    def peek(
        self,
        *,
        inflight: InflightStats,
        project_quota: int,
        skip: set[str] | None = None,
    ) -> QueueEntry | None:
        """下一个可派发条目：跳过 ``skip`` 与项目配额已满者；无可派发返回 None。"""
        skipped = skip or set()
        for entry in self.ordered():
            if entry.job_id in skipped:
                continue
            if project_quota > 0 and entry.project:
                if inflight.per_project.get(entry.project, 0) >= project_quota:
                    continue
            return entry
        return None

    def take(self, job_id: str) -> QueueEntry | None:
        """取走并移出队列（派发成功后调用）。"""
        return self._queue.pop(job_id, None)

    # ------------------------------------------------------------------ 统计
    def _repo(self, model: Any) -> Any:
        """按模型归属取仓储（显式 owner= 模型物理归属；理由见 plugin._repo）。"""
        return self._db.repo(model, owner=MODEL_OWNER)

    async def inflight(self) -> InflightStats:
        """在途（leased/running）Job 的全局 / 按节点 / 按项目计数。"""
        stats = InflightStats()
        jobs = await self._repo(TestJob).list(
            where=(TestJob.state == "leased") | (TestJob.state == "running")
        )
        tasks: dict[str, TestTask] = {}
        for job in jobs:
            stats.total += 1
            stats.per_node[job.node_id] = stats.per_node.get(job.node_id, 0) + 1
            task = tasks.get(job.task_id)
            if task is None:
                task = await self._repo(TestTask).get(job.task_id)
                if task is not None:
                    tasks[job.task_id] = task
            if task is not None:
                project = project_of(_decode_labels(task.labels))
                if project:
                    stats.per_project[project] = stats.per_project.get(project, 0) + 1
        return stats

    async def inflight_jobs(self) -> list[Any]:
        """在途（leased/running）Job 行（租约扫描的输入）。"""
        return await self._repo(TestJob).list(
            where=(TestJob.state == "leased") | (TestJob.state == "running")
        )

    async def jobs_on_node(self, node_id: str) -> list[Any]:
        """某节点上的在途 Job 行（节点失联提前判 lost 用）。"""
        jobs = await self.inflight_jobs()
        return [job for job in jobs if job.node_id == node_id]

    # -------------------------------------------------------------- 节点选择
    def pick_node(
        self,
        executor: ExecutorSelector,
        *,
        inflight: InflightStats,
        avoid: str = "",
    ) -> str | None:
        """为执行器选一个在途最少的节点；无可用节点返回 None。

        * 有 ``ctx.nodes``：``nodes.select(kinds=[executor.kind], labels=None)``
          后按条目 ``kind == executor.kind and id == executor.id`` 且**标签超集**
          （条目 labels 覆盖选择器 labels，§9.2）匹配能力信封；
        * 无 ``ctx.nodes``：降级为 ``ctx.comm.nodes()`` 去掉本节点名，不做能力
          过滤，仅记一次 warning（可选依赖缺席不阻塞核心）。
        """
        candidates = self._candidate_nodes(executor)
        usable = [
            node_id
            for node_id in candidates
            if inflight.per_node.get(node_id, 0) < self._config.max_inflight_per_node
        ]
        if not usable:
            return None
        # 在途少者优先；「换节点优先」：上次执行节点同负载时排后
        usable.sort(key=lambda node: (node == avoid, inflight.per_node.get(node, 0), node))
        return usable[0]

    def _candidate_nodes(self, executor: ExecutorSelector) -> list[str]:
        """能力匹配的候选节点（保序去重）。"""
        service = self._nodes()
        if service is None:
            if not self._no_nodes_warned:
                self._no_nodes_warned = True
                self._log("无 ctx.nodes 服务：降级用 ctx.comm.nodes() 选节点，不做能力过滤")
            self_name = self._self_name()
            names: list[str] = []
            for name in self._comm.nodes():
                text = str(name or "")
                if text and text != self_name and text not in names:
                    names.append(text)
            return names
        names = []
        wanted = dict(executor.labels or {})
        for entry in service.select(kinds=[executor.kind], labels=None):
            entry_labels = entry.get("labels")
            if (
                entry.get("kind") == executor.kind
                and entry.get("id") == executor.id
                and entry.get("node_id")
                and all(
                    isinstance(entry_labels, Mapping)
                    and entry_labels.get(key) == value
                    for key, value in wanted.items()
                )
            ):
                node_id = str(entry["node_id"])
                if node_id not in names:
                    names.append(node_id)
        return names

    # ------------------------------------------------------------ comm 收发
    def send_job(self, node_id: str, descriptor: JobDescriptor, *, ttl: float) -> str:
        """下发 JobDescriptor（dataclass 的 dict 形态）；返回 message_id。"""
        payload = asdict(descriptor)
        return self._comm.send_to(
            node_id,
            TOPIC_JOB,
            payload,
            policy=dict(_AT_LEAST_ONCE),
            ttl=max(1.0, float(ttl)),
        )

    def send_cancel(self, node_id: str, *, job_id: str, attempt: int, reason: str) -> str:
        """下发取消（{job_id, attempt, reason}）；返回 message_id。"""
        return self._comm.send_to(
            node_id,
            TOPIC_JOB_CANCEL,
            {"job_id": job_id, "attempt": attempt, "reason": reason},
            policy=dict(_AT_LEAST_ONCE),
        )

    # ------------------------------------------------------------ 租约扫描
    def lease_expired(self, job: Any, *, now: float, lease_timeout: float) -> bool:
        """租约是否超时（发出 → 结果/续期超过 ``lease_timeout``）。"""
        return (now - float(job.lease_ts)) > float(lease_timeout)

    def task_timeout_of(self, task: Any) -> float:
        """任务总超时秒数：提交规格 ``timeout_s`` 优先，否则配置 ``task_timeout``（0 = 不限）。"""
        try:
            params = json.loads(str(task.params or "{}"))
        except ValueError:
            params = {}
        override = params.get(TASK_TIMEOUT_KEY) if isinstance(params, dict) else None
        if isinstance(override, (int, float)) and not isinstance(override, bool) and override > 0:
            return float(override)
        return float(self._config.task_timeout)

    # -------------------------------------------------------------- 计数器
    def count_lease_timeout(self) -> None:
        """累计一次租约作废（租约超时 / 节点失联提前判 lost）。"""
        self._lease_timeouts_total += 1

    def count_retry(self) -> None:
        """累计一次重派（lost 后 attempt+1 重新入队）。"""
        self._retries_total += 1

    def metrics(self) -> dict[str, int]:
        """调度侧累计指标（/healthz 用）。"""
        return {
            "queue_depth": self.queue_depth(),
            "lease_timeouts_total": self._lease_timeouts_total,
            "retries_total": self._retries_total,
        }


def _decode_labels(raw: str) -> dict[str, str]:
    """task.labels JSON 文本 → dict（坏行降级空对象）。"""
    try:
        data = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


__all__: list[str] = [
    "MODEL_OWNER",
    "TASK_TIMEOUT_KEY",
    "InflightStats",
    "QueueEntry",
    "Scheduler",
    "project_of",
]
