"""test_core 核心插件核心行为测试（DESIGN §5–§11 行为规格）。

覆盖范围（按分组 TestCase，表驱动优先）：

1. 配置：TestCoreConfig 默认值 / 坏值拒绝（负 lease_timeout、bool 冒充 int、max_attempts=0）；
2. 状态机（registry）：迁移表穷举（合法全通 / 非法抛 InvalidTransition、终态出边为零、
   failed 复活仅 rerun）、TASK_FINISHED / JOB_FINISHED 集合语义、validate_task_spec 问题清单；
3. 提交与幂等：submit 落库（task+stages 行、task_history、EVENT_TASK_CREATED 载荷）、
   idempotency_key 去重、task.transform 钩子（改 spec / FAIL 拒绝 / SKIP 放行）；
4. 调度派发：能力匹配（selector.id / labels 超集）、priority 降序 + FIFO、
   max_inflight_jobs / max_inflight_per_node / project_quota 节流、无匹配节点保持 queued；
5. 结果接收：正常结果落库聚合、重复结果幂等、迟到结果不聚合不改状态、
   proto 不匹配判 failed、result.transform 钩子改写 summary/cases；
6. 事件：job-finished / case-finished（仅失败用例、truncated）/ task-finished（counts）/
   artifact-ready（sha256/size）/ task-state（from/to/reason）载荷逐一断言，
   artifact.sink 钩子被调用且异常不影响主流程；
7. 租约与失联：拨钟超 lease_timeout → lost → attempt+1 换节点重派 → 超 max_attempts 判 failed；
   heartbeat/node-status 同步监听器投递失联提前走 lost 路径；
8. 取消：纯 queued 立即 canceled（含 history）、在途进 canceling + TOPIC_JOB_CANCEL、
   结果回报 canceled 后收口、终态任务 cancel 返回 False；
9. 重跑：rerun_failed 同行 attempt+1 回 queued、聚合取最新 attempt、非法状态 / 不存在；
10. 分片：plan job → summary.shards 展开 N 个分片 job（index/total/unit_refs/params 覆盖）
    → 全部回报后屏障推进下一 Stage；shards 缺失建 1 个无分片 job；
11. Stage 组合与策略：线性屏障、fail_fast 首段失败不启动第二段、continue 跑完再判 failed；
12. 查询面：get_task / list_tasks / list_jobs / get_result / list_case_results /
    list_artifacts / mark_artifact；
13. health_metrics：键齐全且计数随行为增长。

测试引导：unittest.IsolatedAsyncioTestCase；每个用例经 ``boot()`` 装最小栈
（appconfig + db + comm + test_core，临时目录独立 sqlite）；FakeNodes 假节点目录
（common.contracts.NodesInterface，能力信封子集匹配）；可控时钟注入 ``core._clock``
（Scheduler 晚绑定）；派发 / 取消经 ``Scheduler._comm`` 换捕获桩记录（traceable
服务代理上方法补丁不生效，故在调度器引用处截获，payload 即 JobDescriptor 的
dict 形态，等价 monkeypatch ctx.comm.send_to）；结果上报直调 ``core._on_job_result``
（另有 comm.report 自环接线用例）；派发循环后台周期被挂起，统一手动 ``tick()``。
"""

from __future__ import annotations

import tempfile
import unittest
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from cordis_port import Context

from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import CommService
from common.plugins.db import DbPlugin
from common.test_contracts import (
    CHAIN_ARTIFACT_SINK,
    CHAIN_RESULT_TRANSFORM,
    CHAIN_TASK_TRANSFORM,
    SCHEMA_VERSION,
    TEST_PROTO,
    TOPIC_JOB,
    TOPIC_JOB_CANCEL,
    TOPIC_JOB_RESULT,
    CaseResult,
    ExecutorSelector,
    InvalidSpec,
    InvalidTransition,
    ShardSpec,
    StageSpec,
    TaskNotFound,
    TaskSpec,
)
from master_plugins.test_core import (
    JOB_FINISHED,
    JOB_TERMINAL,
    JOB_TRANSITIONS,
    TASK_FINISHED,
    TASK_TERMINAL,
    TASK_TRANSITIONS,
    can_transition,
    ensure_valid,
    transition,
    validate_task_spec,
)
from master_plugins.test_core import (
    TestCoreConfig as CoreConfig,
)
from master_plugins.test_core import (
    TestCorePlugin as CorePlugin,
)
from master_plugins.test_core.models import TestTaskHistory as TaskHistory

#: Task 状态字面量全集（§5.1）
TASK_STATES: tuple[str, ...] = (
    "pending",
    "planning",
    "running",
    "canceling",
    "succeeded",
    "failed",
    "canceled",
    "error",
)
#: Job 状态字面量全集（§5.2；上行载荷的 status="timeout" 归 job failed，不是落库状态）
JOB_STATES: tuple[str, ...] = (
    "queued",
    "leased",
    "running",
    "succeeded",
    "failed",
    "canceled",
    "lost",
)

#: 六类广播事件（§11.1，载荷含 schema_version）
EVENT_NAMES: tuple[str, ...] = (
    "test/task-created",
    "test/task-state",
    "test/job-finished",
    "test/case-finished",
    "test/task-finished",
    "test/artifact-ready",
)


# ================================================================ 纯函数辅助
def _cap(
    node_id: str,
    cap_id: str = "pytest",
    *,
    kind: str = "executor",
    labels: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """能力信封条目（FakeNodes 数据源；条目带 node_id，同 NodesInterface）。"""
    return {
        "node_id": node_id,
        "kind": kind,
        "id": cap_id,
        "provider": f"{kind}-{cap_id}",
        "version": "1.0.0",
        "labels": dict(labels or {}),
    }


def _stage(
    name: str = "unit",
    *,
    executor_id: str = "pytest",
    labels: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    shard_mode: str = "none",
    timeout_s: float = 0.0,
) -> StageSpec:
    return StageSpec(
        name=name,
        executor=ExecutorSelector(id=executor_id, labels=dict(labels or {})),
        params=dict(params or {}),
        shard=ShardSpec(mode=shard_mode),  # type: ignore[arg-type]
        timeout_s=timeout_s,
    )


def _spec(
    stages: Sequence[StageSpec] | None = None,
    *,
    name: str = "任务A",
    priority: int = 0,
    labels: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    idempotency_key: str = "",
    failure_policy: str = "fail_fast",
    timeout_s: float = 0.0,
) -> TaskSpec:
    return TaskSpec(
        name=name,
        stages=list(stages) if stages is not None else [_stage()],
        priority=priority,
        labels=dict(labels or {}),
        params=dict(params or {}),
        idempotency_key=idempotency_key,
        failure_policy=failure_policy,  # type: ignore[arg-type]
        timeout_s=timeout_s,
    )


def _case(case_id: str, status: str = "passed", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {"case_id": case_id, "status": status, "duration_s": 0.1}
    row.update(extra)
    return row


def _artifact(artifact_id: str = "art-1", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "artifact_id": artifact_id,
        "kind": "log",
        "name": "run.log",
        "uri": "local://logs/run.log",
        "size": 12,
        "sha256": "abc123",
    }
    row.update(extra)
    return row


def _result_payload(
    job_id: str,
    attempt: int,
    *,
    status: str = "succeeded",
    summary: Mapping[str, Any] | None = None,
    cases: Sequence[Mapping[str, Any]] = (),
    artifacts: Sequence[Mapping[str, Any]] = (),
    proto: int = TEST_PROTO,
    error: str = "",
) -> dict[str, Any]:
    """test.job.result 上行载荷（JobResult 的 dict 形态，§3.1）。"""
    return {
        "proto": proto,
        "job_id": job_id,
        "attempt": attempt,
        "status": status,
        "summary": dict(summary or {}),
        "cases": [dict(item) for item in cases],
        "artifacts": [dict(item) for item in artifacts],
        "started_ts": 1_000.0,
        "finished_ts": 1_001.0,
        "error": error,
    }


class _Clock:
    """可控时间源（租约测试免真实等待，同 test_heartbeat_server 先例）。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeNodes:
    """假节点目录（NodesInterface）：kinds / labels 子集匹配，条目带 node_id。"""

    def __init__(self, caps: Sequence[Mapping[str, object]] = ()) -> None:
        self.caps: list[dict[str, object]] = [dict(entry) for entry in caps]

    def list(self, *, online_only: bool = False) -> list[dict[str, str]]:
        return [
            {
                "node_id": str(entry.get("node_id") or ""),
                "node_kind": "slave",
                "address": "",
                "status": "online",
            }
            for entry in self.caps
        ]

    def select(
        self,
        *,
        kinds: Sequence[str] | None = None,
        labels: Mapping[str, str] | None = None,
        online_only: bool = True,
    ) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for entry in self.caps:
            if kinds is not None and entry.get("kind") not in set(kinds):
                continue
            entry_labels = entry.get("labels") or {}
            if labels is not None and not all(
                entry_labels.get(key) == value for key, value in dict(labels).items()
            ):
                continue
            out.append(dict(entry))
        return out


class _CommSpy:
    """comm 捕获桩：记录 send_to 的 (node, topic, payload)，其余委托真 comm。

    服务经 traceable 代理暴露，方法补丁不传播到调用点（实测），故直接替换
    ``Scheduler._comm`` 引用——捕获点等价 monkeypatch ``ctx.comm.send_to``，
    payload 即 JobDescriptor 经 asdict 展开的 dict。
    """

    def __init__(self, real: Any, sent: list[tuple[str, str, Any]]) -> None:
        self._real = real
        self.sent = sent

    def send_to(
        self,
        node: str,
        topic: str,
        payload: Any,
        *,
        policy: Any = None,
        ttl: float | None = None,
        immediate: bool = False,
    ) -> str:
        self.sent.append((str(node), str(topic), payload))
        return f"spy-{len(self.sent)}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


# ================================================================ 栈基类
class TestCoreStackTestCase(unittest.IsolatedAsyncioTestCase):
    """test_core 最小栈基类：``boot()`` 按用例装配，可控时钟 + comm 捕获。"""

    context: Context | None

    async def asyncSetUp(self) -> None:
        # 派发循环改手动 tick()：挂起 1s 周期的后台派发，杜绝并发双派偶发
        self._interval_patch = mock.patch(
            "master_plugins.test_core.plugin._DISPATCH_INTERVAL", 3600.0
        )
        self._interval_patch.start()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.clock = _Clock()
        self.sent: list[tuple[str, str, Any]] = []
        self.events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.context = None
        self.core: Any = None
        self.nodes: FakeNodes | None = None

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
            self.context = None
        self._interval_patch.stop()
        self._temp.cleanup()

    async def boot(
        self,
        *,
        config: Mapping[str, object] | None = None,
        caps: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        """装配 appconfig + db + comm + test_core（单节点 node-a 缺省能力）。"""
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            "name = master-node\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n",
            encoding="utf-8",
        )
        context = Context()
        self.context = context
        context.provide("node_kind", "master")
        context.provide("node_name", "master-node")
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await context.plugin(DbPlugin)
        await context.plugin(CommService)
        self.nodes = FakeNodes(list(caps) if caps is not None else [_cap("node-a")])
        context.provide("nodes", self.nodes)
        for name in EVENT_NAMES:
            context.on(name, self._make_collector(name))
        await context.plugin(CorePlugin, dict(config or {}))
        self.core = context.test_core
        self.core._clock = self.clock
        self.core._scheduler._comm = _CommSpy(context.comm, self.sent)

    def _make_collector(self, name: str) -> Callable[[Any], None]:
        def collect(payload: Any) -> None:
            if isinstance(payload, Mapping):
                self.events[name].append(dict(payload))

        return collect

    # ------------------------------------------------------------ 驱动辅助
    async def submit_spec(self, spec: TaskSpec, *, source: str = "api") -> str:
        return await self.core.submit(spec, source=source)

    async def tick(self) -> None:
        """手动跑一轮派发（后台循环已挂起，1s 周期不参与测试）。"""
        await self.core._dispatch_tick()

    async def sweep(self) -> None:
        """手动跑一轮租约扫描（失联投递 / 租约超时判定）。"""
        await self.core._sweep_once()

    async def send_result(self, job_id: str, attempt: int, **kwargs: Any) -> None:
        """经收件主路径回报一次 test.job.result（直调 _on_job_result）。"""
        await self.core._on_job_result(_result_payload(job_id, attempt, **kwargs), None)

    async def jobs_of(self, task_id: str) -> list[dict[str, Any]]:
        return list(await self.core.list_jobs(task_id))

    def job_messages(self) -> list[tuple[str, dict[str, Any]]]:
        return [(node, payload) for node, topic, payload in self.sent if topic == TOPIC_JOB]

    def cancel_messages(self) -> list[dict[str, Any]]:
        return [payload for _node, topic, payload in self.sent if topic == TOPIC_JOB_CANCEL]

    async def task_state(self, task_id: str) -> str:
        detail = await self.core.get_task(task_id)
        assert detail is not None
        return str(detail["state"])

    async def history_rows(self, task_id: str) -> list[TaskHistory]:
        return await self.core._repo(TaskHistory).list(
            where=TaskHistory.task_id == task_id,
            order_by=[TaskHistory.ts, TaskHistory.id],
        )


# ================================================================ 1. 配置
class ConfigTests(unittest.TestCase):
    """TestCoreConfig 默认值与坏值拒绝（DESIGN §13）。"""

    def test_defaults_match_design(self) -> None:
        config = CoreConfig()
        self.assertEqual(config.lease_timeout, 300.0)
        self.assertEqual(config.job_timeout, 3600.0)
        self.assertEqual(config.task_timeout, 0.0)
        self.assertEqual(config.max_attempts, 3)
        self.assertEqual(config.max_inflight_jobs, 32)
        self.assertEqual(config.max_inflight_per_node, 4)
        self.assertEqual(config.default_priority, 0)
        self.assertEqual(config.project_quota, 0)
        self.assertEqual(config.case_results_limit, 2000)
        self.assertEqual(config.retention_days, 30)
        self.assertEqual(config.history_size, 200)

    def test_valid_values_normalize(self) -> None:
        result = CoreConfig.validate({"lease_timeout": 5, "max_attempts": 2})
        config = result.get("value")
        self.assertIsInstance(config, CoreConfig)
        self.assertEqual(config.lease_timeout, 5.0)  # 归一化为 float
        self.assertEqual(config.max_attempts, 2)

    def test_rejects_bad_values(self) -> None:
        bad: list[tuple[dict[str, object], str]] = [
            ({"lease_timeout": -5}, "lease_timeout"),  # 负租约
            ({"lease_timeout": True}, "lease_timeout"),  # bool 冒充数
            ({"lease_timeout": float("inf")}, "lease_timeout"),  # 非有限
            ({"job_timeout": 0}, "job_timeout"),  # 须为正数
            ({"task_timeout": -1}, "task_timeout"),  # 须非负
            ({"max_attempts": 0}, "max_attempts"),  # 须 ≥ 1
            ({"max_attempts": True}, "max_attempts"),  # bool 冒充 int
            ({"max_inflight_jobs": -1}, "max_inflight_jobs"),
            ({"history_size": "many"}, "history_size"),  # 类型错
        ]
        for values, path in bad:
            with self.subTest(values=values):
                result = CoreConfig.validate(values)
                issues = result.get("issues")
                self.assertIsInstance(issues, list, f"期望拒绝 {values}，实得 {result}")
                paths = [issue.get("path") for issue in issues]  # type: ignore[union-attr]
                self.assertIn([path], paths)


# ================================================================ 2. 状态机
class TaskTransitionTests(unittest.TestCase):
    """Task 迁移表穷举（§5.1）：合法全通、非法全抛、终态出边为零。"""

    def test_table_matches_spec(self) -> None:
        self.assertEqual(TASK_TRANSITIONS["pending"], frozenset({"planning", "running", "canceling"}))
        self.assertEqual(TASK_TRANSITIONS["planning"], frozenset({"running", "canceling", "error"}))
        self.assertEqual(TASK_TRANSITIONS["running"], frozenset({"succeeded", "failed", "error", "canceling"}))
        self.assertEqual(TASK_TRANSITIONS["canceling"], frozenset({"canceled", "error"}))
        self.assertEqual(TASK_TRANSITIONS["failed"], frozenset({"running"}))
        self.assertEqual(TASK_TERMINAL, frozenset({"succeeded", "canceled", "error"}))
        self.assertEqual(TASK_FINISHED, frozenset({"succeeded", "failed", "canceled", "error"}))

    def test_exhaustive_transitions(self) -> None:
        for current in TASK_STATES:
            targets = TASK_TRANSITIONS.get(current, frozenset())
            for target in TASK_STATES:
                with self.subTest(current=current, target=target):
                    self.assertEqual(can_transition("task", current, target), target in targets)
                    if target in targets:
                        self.assertEqual(transition("task", current, target, reason="测试"), target)
                    else:
                        with self.assertRaises(InvalidTransition):
                            transition("task", current, target, reason="测试")

    def test_terminal_states_have_no_exits_and_failed_revives_only_via_rerun(self) -> None:
        for state in TASK_TERMINAL:
            self.assertEqual(TASK_TRANSITIONS[state], frozenset())
        # failed 不是终态：唯一复活通道 rerun_failed（failed → running）
        self.assertEqual(TASK_TRANSITIONS["failed"], frozenset({"running"}))
        self.assertIn("failed", TASK_FINISHED)
        self.assertNotIn("failed", TASK_TERMINAL)


class JobTransitionTests(unittest.TestCase):
    """Job 迁移表穷举（§5.2）。"""

    def test_table_matches_spec(self) -> None:
        self.assertEqual(JOB_TRANSITIONS["queued"], frozenset({"leased", "canceled"}))
        self.assertEqual(
            JOB_TRANSITIONS["leased"],
            frozenset({"running", "succeeded", "failed", "canceled", "lost"}),
        )
        self.assertEqual(
            JOB_TRANSITIONS["running"],
            frozenset({"succeeded", "failed", "canceled", "lost"}),
        )
        self.assertEqual(JOB_TRANSITIONS["lost"], frozenset({"queued", "failed"}))
        self.assertEqual(JOB_TRANSITIONS["failed"], frozenset({"queued"}))
        self.assertEqual(JOB_TERMINAL, frozenset({"succeeded", "canceled"}))
        self.assertEqual(JOB_FINISHED, frozenset({"succeeded", "failed", "canceled"}))

    def test_exhaustive_transitions(self) -> None:
        for current in JOB_STATES:
            targets = JOB_TRANSITIONS.get(current, frozenset())
            for target in JOB_STATES:
                with self.subTest(current=current, target=target):
                    self.assertEqual(can_transition("job", current, target), target in targets)
                    if target in targets:
                        self.assertEqual(transition("job", current, target, reason="测试"), target)
                    else:
                        with self.assertRaises(InvalidTransition):
                            transition("job", current, target, reason="测试")

    def test_rerun_revival_and_finished_sets(self) -> None:
        # failed → queued 仅 rerun；其余出边一律非法
        self.assertTrue(can_transition("job", "failed", "queued"))
        for target in ("leased", "running", "succeeded", "canceled", "lost"):
            self.assertFalse(can_transition("job", "failed", target))
        # 终态出边为零
        for state in JOB_TERMINAL:
            self.assertEqual(JOB_TRANSITIONS[state], frozenset())
        # lost 不算完结（还在重派窗口），failed 算完结（复活仅 rerun）
        self.assertNotIn("lost", JOB_FINISHED)
        self.assertIn("failed", JOB_FINISHED)
        self.assertNotIn("failed", JOB_TERMINAL)


class ValidateSpecTests(unittest.TestCase):
    """validate_task_spec 问题清单（纯函数、不抛）。"""

    def test_valid_spec_has_no_problems(self) -> None:
        self.assertEqual(validate_task_spec(_spec()), [])
        self.assertEqual(
            validate_task_spec(
                _spec([_stage("unit", shard_mode="plan"), _stage("report", timeout_s=30.0)], timeout_s=60.0)
            ),
            [],
        )

    def test_problem_list(self) -> None:
        cases: list[tuple[TaskSpec, str]] = [
            (_spec([]), "stages 不能为空"),
            (_spec([_stage("unit"), _stage("unit")]), "重复"),
            (_spec([_stage("s", shard_mode="bogus")]), "shard.mode"),
            (_spec([_stage("s", timeout_s=-1.0)]), "timeout_s"),
            (_spec(timeout_s=float("nan")), "timeout_s"),
            (_spec(timeout_s=True), "timeout_s"),  # type: ignore[arg-type]
            (_spec([_stage("")]), "name"),
            (_spec([_stage("s", executor_id="")]), "executor.id"),
            (_spec([], name=""), "name"),
            (_spec([], priority=True), "priority"),  # type: ignore[arg-type]
            (_spec([], failure_policy="maybe"), "failure_policy"),  # type: ignore[arg-type]
        ]
        for spec, keyword in cases:
            with self.subTest(keyword=keyword):
                problems = validate_task_spec(spec)
                self.assertTrue(problems, f"期望有问题（{keyword}），实得空清单")
                self.assertTrue(
                    any(keyword in problem for problem in problems),
                    f"问题清单缺少 {keyword!r}：{problems}",
                )

    def test_ensure_valid_raises_invalid_spec(self) -> None:
        with self.assertRaises(InvalidSpec):
            ensure_valid(_spec([]))
        ensure_valid(_spec([_stage()]))  # 合法静默通过


# ================================================================ 3. 提交与幂等
class SubmitTests(TestCoreStackTestCase):
    """submit 落库、幂等提交与 task.transform 钩子（§6 / §11.2）。"""

    async def test_submit_persists_rows_history_and_event(self) -> None:
        await self.boot()
        spec = _spec(
            [_stage("unit", shard_mode="plan"), _stage("report")],
            name="夜间回归",
            priority=7,
            labels={"project": "p1"},
            params={"env": "ci"},
        )
        task_id = await self.submit_spec(spec)

        detail = await self.core.get_task(task_id)
        assert detail is not None
        self.assertEqual(detail["task_id"], task_id)
        self.assertEqual(detail["name"], "夜间回归")
        self.assertEqual(detail["priority"], 7)
        self.assertEqual(detail["labels"], {"project": "p1"})
        self.assertEqual(detail["params"], {"env": "ci"})
        self.assertEqual(detail["state"], "planning")  # 首段 plan 型 → planning
        self.assertEqual(detail["current_stage"], "unit")
        self.assertEqual(detail["failure_policy"], "fail_fast")
        self.assertEqual(detail["created_ts"], self.clock.now)
        stages = detail["stages"]
        self.assertEqual([s["name"] for s in stages], ["unit", "report"])
        self.assertEqual([s["seq"] for s in stages], [1, 2])
        self.assertEqual(stages[0]["shard_mode"], "plan")
        self.assertEqual(stages[1]["shard_mode"], "none")
        self.assertEqual(stages[0]["executor"]["id"], "pytest")

        # task_history：pending → planning（reason=submit）
        history = await self.history_rows(task_id)
        self.assertEqual(
            [(row.from_state, row.to_state, row.reason) for row in history],
            [("pending", "planning", "submit")],
        )

        # EVENT_TASK_CREATED 载荷（含 schema_version）
        created = self.events["test/task-created"]
        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0],
            {
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "name": "夜间回归",
                "labels": {"project": "p1"},
                "source": "api",
                "priority": 7,
            },
        )

    async def test_idempotency_key_dedupes_submission(self) -> None:
        await self.boot()
        spec = _spec([_stage(), _stage("report")], idempotency_key="build-42")
        first = await self.submit_spec(spec)
        second = await self.submit_spec(spec)
        self.assertEqual(first, second)
        self.assertEqual(len(await self.core.list_tasks()), 1)
        detail = await self.core.get_task(first)
        assert detail is not None
        self.assertEqual(len(detail["stages"]), 2)  # 不重复建行
        self.assertEqual(len(self.events["test/task-created"]), 1)

        other = await self.submit_spec(_spec(idempotency_key="build-43"))
        self.assertNotEqual(other, first)
        self.assertEqual(len(await self.core.list_tasks()), 2)

    async def test_task_transform_hook_modifies_spec(self) -> None:
        await self.boot()
        seen: list[TaskSpec] = []

        async def decorate(ctx: Any, nxt: Any) -> Any:
            seen.append(ctx.spec)
            ctx.spec = replace(ctx.spec, labels={**dict(ctx.spec.labels), "env": "staging"})
            return await nxt()

        self.core.register_hook(
            CHAIN_TASK_TRANSFORM, decorate, phase="transform", order=10, name="decorate"
        )
        task_id = await self.submit_spec(_spec(labels={"project": "p1"}))
        self.assertEqual(len(seen), 1)
        detail = await self.core.get_task(task_id)
        assert detail is not None
        self.assertEqual(detail["labels"], {"project": "p1", "env": "staging"})

    async def test_task_transform_fail_strategy_rejects_submit(self) -> None:
        await self.boot()

        async def boom(ctx: Any, nxt: Any) -> Any:
            raise RuntimeError("transform 失败")

        self.core.register_hook(
            CHAIN_TASK_TRANSFORM, boom, phase="guard", order=10, name="boom", on_error="fail"
        )
        with self.assertRaises(RuntimeError):
            await self.submit_spec(_spec())
        self.assertEqual(await self.core.list_tasks(), [])
        self.assertEqual(self.events["test/task-created"], [])

    async def test_task_transform_skip_strategy_passes(self) -> None:
        await self.boot()

        async def boom(ctx: Any, nxt: Any) -> Any:
            raise RuntimeError("统计钩子坏了")

        self.core.register_hook(
            CHAIN_TASK_TRANSFORM, boom, phase="observe", order=10, name="boom", on_error="skip"
        )
        task_id = await self.submit_spec(_spec())
        self.assertEqual(await self.task_state(task_id), "running")
        self.assertEqual(len(self.events["test/task-created"]), 1)


# ================================================================ 4. 调度派发
class DispatchTests(TestCoreStackTestCase):
    """能力匹配、优先级出队、背压节流与无节点兜底（§9）。"""

    async def test_dispatch_to_capability_matched_node(self) -> None:
        await self.boot(caps=[_cap("node-a"), _cap("node-b")])
        task_id = await self.submit_spec(_spec(params={"targets": ["t/"]}, labels={"project": "p1"}))
        await self.tick()
        sends = self.job_messages()
        self.assertEqual(len(sends), 1)
        node, descriptor = sends[0]
        self.assertEqual(node, "node-a")  # 在途同为 0 时按节点名序
        self.assertEqual(descriptor["proto"], TEST_PROTO)
        self.assertEqual(descriptor["task_id"], task_id)
        self.assertEqual(descriptor["stage_name"], "unit")
        self.assertEqual(descriptor["attempt"], 1)
        self.assertEqual(descriptor["executor"]["id"], "pytest")
        self.assertEqual(descriptor["params"], {"targets": ["t/"]})
        self.assertIsNone(descriptor["shard"])
        self.assertIn(f"/{task_id}/", descriptor["artifact_dir"])
        self.assertTrue(descriptor["artifact_dir"].endswith("/a1"))
        self.assertEqual(descriptor["deadline_ts"], self.clock.now + 3600.0)
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "leased")
        self.assertEqual(jobs[0]["node_id"], "node-a")

    async def test_selector_id_mismatch_not_dispatched(self) -> None:
        await self.boot(caps=[_cap("node-a", cap_id="other")])
        task_id = await self.submit_spec(_spec())
        await self.tick()
        self.assertEqual(self.job_messages(), [])
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "queued")

    async def test_selector_labels_mismatch_not_dispatched(self) -> None:
        await self.boot(caps=[_cap("node-a", labels={"lang": "python"})])
        task_id = await self.submit_spec(_spec([_stage(labels={"lang": "go"})]))
        await self.tick()
        self.assertEqual(self.job_messages(), [])
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "queued")

    async def test_selector_labels_superset_matches(self) -> None:
        await self.boot(caps=[_cap("node-a", labels={"lang": "python", "os": "linux"})])
        task_id = await self.submit_spec(_spec([_stage(labels={"lang": "python"})]))
        await self.tick()
        self.assertEqual(len(self.job_messages()), 1)
        self.assertEqual((await self.jobs_of(task_id))[0]["state"], "leased")

    async def test_priority_desc_then_fifo(self) -> None:
        await self.boot(caps=[_cap("node-a"), _cap("node-b")])
        low = await self.submit_spec(_spec(name="低", priority=0))
        high = await self.submit_spec(_spec(name="高", priority=5))
        await self.tick()
        dispatched = [payload["task_id"] for _node, payload in self.job_messages()]
        self.assertEqual(dispatched, [high, low])

    async def test_fifo_same_priority(self) -> None:
        await self.boot(caps=[_cap("node-a"), _cap("node-b")])
        first = await self.submit_spec(_spec(name="先"))
        second = await self.submit_spec(_spec(name="后"))
        await self.tick()
        dispatched = [payload["task_id"] for _node, payload in self.job_messages()]
        self.assertEqual(dispatched, [first, second])

    async def test_max_inflight_jobs_throttles(self) -> None:
        await self.boot(config={"max_inflight_jobs": 1})
        await self.submit_spec(_spec(name="t1"))
        await self.submit_spec(_spec(name="t2"))
        await self.tick()
        self.assertEqual(len(self.job_messages()), 1)
        await self.tick()
        self.assertEqual(len(self.job_messages()), 1)  # 窗口满不再派

    async def test_max_inflight_per_node_throttles(self) -> None:
        await self.boot(config={"max_inflight_per_node": 1}, caps=[_cap("node-a")])
        assert self.nodes is not None
        t1 = await self.submit_spec(_spec(name="t1"))
        t2 = await self.submit_spec(_spec(name="t2"))
        await self.tick()
        self.assertEqual(len(self.job_messages()), 1)
        self.assertEqual((await self.jobs_of(t1))[0]["state"], "leased")
        self.assertEqual((await self.jobs_of(t2))[0]["state"], "queued")  # 单节点窗口满保持排队
        # 第二个节点接入后补派
        self.nodes.caps.append(_cap("node-b"))
        await self.tick()
        self.assertEqual([node for node, _p in self.job_messages()], ["node-a", "node-b"])

    async def test_project_quota_throttles(self) -> None:
        await self.boot(config={"project_quota": 1})
        await self.submit_spec(_spec(name="t1", labels={"project": "p1"}))
        await self.submit_spec(_spec(name="t2", labels={"project": "p1"}))
        await self.submit_spec(_spec(name="t3", labels={"project": "p2"}))
        await self.tick()
        dispatched = {payload["task_id"] for _node, payload in self.job_messages()}
        self.assertEqual(len(dispatched), 2)  # p1 配额 1 + p2 配额 1

    async def test_no_matching_node_keeps_queued(self) -> None:
        await self.boot(caps=[])
        task_id = await self.submit_spec(_spec())
        await self.tick()
        self.assertEqual(self.job_messages(), [])
        self.assertEqual((await self.jobs_of(task_id))[0]["state"], "queued")
        self.assertEqual(await self.task_state(task_id), "running")


# ================================================================ 5. 结果接收
class ResultTests(TestCoreStackTestCase):
    """结果落库聚合、幂等、proto 握手与 result.transform（§5.2 / §8.3）。"""

    async def test_succeeded_result_persists_and_aggregates(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        job_id = descriptor["job_id"]
        await self.send_result(
            job_id,
            1,
            summary={"total": 3, "passed": 2, "failed": 1},
            cases=[
                _case("c1", "passed"),
                _case("c2", "failed", message="boom", detail_ref="art-1"),
            ],
            artifacts=[_artifact("art-1")],
        )

        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "succeeded")
        results = jobs[0]["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], f"{job_id}:1")
        self.assertEqual(results[0]["status"], "succeeded")
        self.assertEqual(results[0]["summary"], {"total": 3, "passed": 2, "failed": 1})

        cases = await self.core.list_case_results(task_id=task_id)
        self.assertEqual({row["case_id"] for row in cases}, {"c1", "c2"})
        self.assertEqual({row["status"] for row in cases}, {"passed", "failed"})

        artifacts = await self.core.list_artifacts(task_id=task_id)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["artifact_id"], "art-1")
        self.assertEqual(artifacts[0]["state"], "local")

        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"total": 3, "passed": 2, "failed": 1})
        self.assertEqual(result["cases"], {"passed": 1, "failed": 1})
        self.assertEqual(result["artifacts"], 1)
        self.assertEqual(result["state"], "succeeded")

    async def test_failed_result_marks_job_and_task_failed(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(
            descriptor["job_id"], 1, status="failed", error="exec boom", summary={"failed": 2}
        )
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "failed")
        self.assertEqual(jobs[0]["error"], "exec boom")
        self.assertEqual(await self.task_state(task_id), "failed")

    async def test_duplicate_result_is_idempotent(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        payload = _result_payload(
            descriptor["job_id"], 1, summary={"passed": 2}, cases=[_case("c1")]
        )
        await self.core._on_job_result(payload, None)
        await self.core._on_job_result(payload, None)

        self.assertEqual(self.core._results_duplicate_total, 1)
        jobs = await self.jobs_of(task_id)
        self.assertEqual(len(jobs[0]["results"]), 1)  # 不重复建行
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"passed": 2})  # 不重复聚合
        self.assertEqual(len(await self.core.list_case_results(task_id=task_id)), 1)

    async def test_proto_mismatch_fails_job(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(descriptor["job_id"], 1, proto=TEST_PROTO + 1)
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "failed")
        self.assertIn("proto mismatch", jobs[0]["error"])
        self.assertEqual(len(jobs[0]["results"]), 1)  # 握手失败也留证
        self.assertEqual(await self.task_state(task_id), "failed")

    async def test_result_transform_hook_modifies_summary_and_cases(self) -> None:
        await self.boot()

        async def normalize(ctx: Any, nxt: Any) -> Any:
            ctx.result = replace(
                ctx.result,
                summary={"passed": 99},
                cases=[CaseResult(case_id="renamed", status="passed", duration_s=1.0)],
            )
            return await nxt()

        self.core.register_hook(
            CHAIN_RESULT_TRANSFORM, normalize, phase="transform", order=10, name="normalize"
        )
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(descriptor["job_id"], 1, summary={"passed": 1}, cases=[_case("raw")])
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["results"][0]["summary"], {"passed": 99})
        cases = await self.core.list_case_results(task_id=task_id)
        self.assertEqual([row["case_id"] for row in cases], ["renamed"])
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"passed": 99})

    async def test_report_via_comm_loops_back(self) -> None:
        """结果上报的接线用例：comm.report 自环进 _on_job_result。"""
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        assert self.context is not None
        self.context.comm.report(
            TOPIC_JOB_RESULT,
            _result_payload(descriptor["job_id"], 1, summary={"passed": 1}),
        )
        found = await wait_until(
            lambda: any(
                event.get("job_id") == descriptor["job_id"]
                for event in self.events["test/job-finished"]
            ),
            timeout=5.0,
        )
        self.assertTrue(found)
        self.assertEqual((await self.jobs_of(task_id))[0]["state"], "succeeded")


class LateResultTests(TestCoreStackTestCase):
    """迟到结果（attempt 小于当前）：落库不聚合不改状态（§5.2）。"""

    async def test_late_result_stored_but_not_aggregated(self) -> None:
        await self.boot(config={"lease_timeout": 60.0, "max_attempts": 2})
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        job_id = descriptor["job_id"]
        # 拨钟超租约 → lost → attempt+1 回 queued
        self.clock.advance(61.0)
        await self.sweep()
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["attempt"], 2)
        self.assertEqual(jobs[0]["state"], "queued")

        # attempt 1 的迟到上报：落库、不聚合、不改状态
        await self.send_result(job_id, 1, status="succeeded", summary={"passed": 7})
        self.assertEqual(self.core._late_results_total, 1)
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "queued")
        self.assertEqual(len(jobs[0]["results"]), 1)  # job_result 每 attempt 一行
        self.assertEqual(jobs[0]["results"][0]["attempt"], 1)
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {})  # 只认最新 attempt

        # attempt 2 正常上报才聚合
        await self.tick()
        await self.send_result(job_id, 2, status="succeeded", summary={"passed": 5})
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"passed": 5})
        self.assertEqual((await self.jobs_of(task_id))[0]["state"], "succeeded")


# ================================================================ 6. 事件
class EventTests(TestCoreStackTestCase):
    """六类事件载荷逐一断言 + artifact.sink 旁路钩子（§11.1 / §11.2）。"""

    async def test_job_case_artifact_event_payloads(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        job_id = descriptor["job_id"]
        await self.send_result(
            job_id,
            1,
            summary={"passed": 1},
            cases=[
                _case("c1", "failed", message="m1", detail_ref="d1"),
                _case("c2", "passed"),
                _case("c3", "error", message="m3", detail_ref="d3"),
            ],
            artifacts=[_artifact("art-1", size=12, sha256="abc123")],
        )

        finished = self.events["test/job-finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(
            finished[0],
            {
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "job_id": job_id,
                "attempt": 1,
                "status": "succeeded",
                "node_id": "node-a",
                "summary": {"passed": 1},
            },
        )

        case_events = self.events["test/case-finished"]
        self.assertEqual(len(case_events), 1)
        self.assertEqual(case_events[0]["task_id"], task_id)
        self.assertEqual(case_events[0]["job_id"], job_id)
        self.assertEqual(case_events[0]["attempt"], 1)
        self.assertFalse(case_events[0]["truncated"])
        # 仅失败 / 错误用例进批（passed 不进）
        self.assertEqual(
            case_events[0]["failed"],
            [
                {"case_id": "c1", "message": "m1", "detail_ref": "d1"},
                {"case_id": "c3", "message": "m3", "detail_ref": "d3"},
            ],
        )

        ready = self.events["test/artifact-ready"]
        self.assertEqual(len(ready), 1)
        self.assertEqual(
            ready[0],
            {
                "schema_version": SCHEMA_VERSION,
                "artifact_id": "art-1",
                "task_id": task_id,
                "job_id": job_id,
                "kind": "log",
                "uri": "local://logs/run.log",
                "size": 12,
                "sha256": "abc123",
            },
        )

    async def test_task_state_and_task_finished_payloads(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        states = self.events["test/task-state"]
        self.assertEqual(
            [(row["from"], row["to"], row["reason"]) for row in states],
            [("pending", "running", "submit")],
        )
        self.assertEqual(states[0]["schema_version"], SCHEMA_VERSION)
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(descriptor["job_id"], 1, summary={"total": 3, "passed": 3})

        states = self.events["test/task-state"]
        self.assertEqual(states[-1]["from"], "running")
        self.assertEqual(states[-1]["to"], "succeeded")
        self.assertEqual(states[-1]["reason"], "all stages finished")

        done = self.events["test/task-finished"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["schema_version"], SCHEMA_VERSION)
        self.assertEqual(done[0]["task_id"], task_id)
        self.assertEqual(done[0]["state"], "succeeded")
        self.assertEqual(done[0]["counts"], {"total": 3, "passed": 3})
        self.assertGreaterEqual(done[0]["duration_s"], 0.0)

    async def test_case_event_truncated_semantics(self) -> None:
        await self.boot(config={"case_results_limit": 2})
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(
            descriptor["job_id"],
            1,
            cases=[_case(f"c{i}", "failed", message=f"m{i}") for i in range(4)],
        )
        case_events = self.events["test/case-finished"]
        self.assertTrue(case_events[0]["truncated"])  # 4 例 > 上限 2
        self.assertEqual(len(case_events[0]["failed"]), 4)  # 事件批上限 200，未触顶
        rows = await self.core.list_case_results(task_id=task_id)
        self.assertEqual(len(rows), 2)  # 落库截断

    async def test_artifact_sink_called_and_error_isolated(self) -> None:
        await self.boot()
        seen: list[Any] = []

        async def audit(ref_ctx: Any, nxt: Any) -> Any:
            seen.append(ref_ctx.ref)
            return await nxt()

        async def boom(ref_ctx: Any, nxt: Any) -> Any:
            raise RuntimeError("旁路写挂了")

        self.core.register_hook(CHAIN_ARTIFACT_SINK, audit, phase="transform", order=10, name="audit")
        self.core.register_hook(
            CHAIN_ARTIFACT_SINK, boom, phase="transform", order=20, name="boom", on_error="fail"
        )
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(descriptor["job_id"], 1, artifacts=[_artifact("art-1")])

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].artifact_id, "art-1")  # 只读 ArtifactRef
        self.assertEqual((await self.jobs_of(task_id))[0]["state"], "succeeded")  # 钩子异常不影响主流程
        self.assertEqual(await self.task_state(task_id), "succeeded")


# ================================================================ 7. 租约与失联
class LeaseTests(TestCoreStackTestCase):
    """租约超时重派换节点、超限判 failed、节点失联提前 lost（§10）。"""

    caps: ClassVar[list[dict[str, object]]] = [_cap("node-a"), _cap("node-b")]

    async def boot_lease(self) -> None:
        await self.boot(config={"lease_timeout": 60.0, "max_attempts": 2}, caps=type(self).caps)

    async def test_lease_timeout_requeues_onto_other_node(self) -> None:
        await self.boot_lease()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        first = self.job_messages()[0]
        self.assertEqual(first[0], "node-a")

        self.clock.advance(61.0)  # > lease_timeout
        await self.sweep()
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "queued")
        self.assertEqual(jobs[0]["attempt"], 2)

        await self.tick()
        sends = self.job_messages()
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[1][0], "node-b")  # 换节点优先：避开 node-a
        self.assertEqual(sends[1][1]["attempt"], 2)
        metrics = self.core.health_metrics()
        self.assertEqual(metrics["lease_timeouts_total"], 1)
        self.assertEqual(metrics["retries_total"], 1)

    async def test_lease_timeout_exhausts_max_attempts(self) -> None:
        await self.boot_lease()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        self.clock.advance(61.0)
        await self.sweep()  # attempt 1 → 2 重派
        await self.tick()
        self.clock.advance(61.0)
        await self.sweep()  # attempt 2 已达上限 → failed

        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "failed")
        self.assertEqual(jobs[0]["attempt"], 2)
        self.assertEqual(await self.task_state(task_id), "failed")
        self.assertEqual(len(self.events["test/job-finished"]), 1)

    async def test_node_status_lost_takes_lost_path_early(self) -> None:
        await self.boot_lease()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        self.assertEqual(self.job_messages()[0][0], "node-a")

        # heartbeat/node-status 同步监听器投递失联（不拨钟、租约未到期）
        self.core._on_node_status({"node_id": "node-a", "to": "offline"})
        await self.sweep()
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "queued")
        self.assertEqual(jobs[0]["attempt"], 2)
        await self.tick()
        self.assertEqual(self.job_messages()[1][0], "node-b")

        # online 转换不触发 lost
        self.core._on_node_status({"node_id": "node-b", "to": "online"})
        await self.sweep()
        self.assertEqual((await self.jobs_of(task_id))[0]["state"], "leased")


# ================================================================ 8. 取消
class CancelTests(TestCoreStackTestCase):
    """任务取消：纯排队即收口 / 在途发 TOPIC_JOB_CANCEL / 终态拒绝（§5.1）。"""

    async def test_cancel_queued_task_finishes_immediately(self) -> None:
        await self.boot(caps=[])
        task_id = await self.submit_spec(_spec())
        self.assertTrue(await self.core.cancel(task_id, reason="用户取消"))

        self.assertEqual(await self.task_state(task_id), "canceled")
        detail = await self.core.get_task(task_id)
        assert detail is not None
        self.assertEqual(detail["error"], "")
        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["state"], "canceled")
        history = await self.history_rows(task_id)
        # 拨钟冻结下同 ts 的行序不稳定（id 随机尾），按内容比对
        self.assertCountEqual(
            [(row.from_state, row.to_state) for row in history],
            [("pending", "running"), ("running", "canceling"), ("canceling", "canceled")],
        )
        self.assertEqual(
            [row.reason for row in history if row.to_state == "canceling"], ["用户取消"]
        )
        done = self.events["test/task-finished"]
        self.assertEqual(len(done), 1)  # 收口只记一次（屏障 + 兜底幂等）
        self.assertEqual(done[-1]["state"], "canceled")

    async def test_cancel_inflight_sends_cancel_then_finishes_on_result(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]

        self.assertTrue(await self.core.cancel(task_id, reason="不跑了"))
        self.assertEqual(await self.task_state(task_id), "canceling")
        cancels = self.cancel_messages()
        self.assertEqual(len(cancels), 1)
        self.assertEqual(
            cancels[0],
            {"job_id": descriptor["job_id"], "attempt": 1, "reason": "不跑了"},
        )

        # 执行器回报 canceled 后收口
        await self.send_result(descriptor["job_id"], 1, status="canceled")
        self.assertEqual((await self.jobs_of(task_id))[0]["state"], "canceled")
        self.assertEqual(await self.task_state(task_id), "canceled")

    async def test_cancel_finished_task_returns_false(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(descriptor["job_id"], 1)
        self.assertEqual(await self.task_state(task_id), "succeeded")
        self.assertFalse(await self.core.cancel(task_id))
        self.assertFalse(await self.core.cancel("t-missing"))


# ================================================================ 9. 重跑
class RerunTests(TestCoreStackTestCase):
    """rerun_failed：同行 attempt+1 回 queued，聚合取最新 attempt（§5.2）。"""

    async def test_rerun_failed_requeues_same_job(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        job_id = descriptor["job_id"]
        await self.send_result(job_id, 1, status="failed", summary={"passed": 1}, error="boom")
        self.assertEqual(await self.task_state(task_id), "failed")

        self.assertEqual(await self.core.rerun_failed(task_id), task_id)
        self.assertEqual(await self.task_state(task_id), "running")
        jobs = await self.jobs_of(task_id)
        self.assertEqual(len(jobs), 1)  # 同一 Job 行，不另建
        self.assertEqual(jobs[0]["job_id"], job_id)
        self.assertEqual(jobs[0]["state"], "queued")
        self.assertEqual(jobs[0]["attempt"], 2)

        await self.tick()
        self.assertEqual(self.job_messages()[1][1]["attempt"], 2)
        await self.send_result(job_id, 2, status="succeeded", summary={"passed": 5})

        self.assertEqual(await self.task_state(task_id), "succeeded")
        jobs = await self.jobs_of(task_id)
        self.assertEqual(len(jobs[0]["results"]), 2)  # 每 attempt 一行
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"passed": 5})  # 聚合取最新 attempt

    async def test_rerun_non_failed_task_raises(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(descriptor["job_id"], 1)
        with self.assertRaises(InvalidTransition):
            await self.core.rerun_failed(task_id)

    async def test_rerun_missing_task_raises(self) -> None:
        await self.boot()
        with self.assertRaises(TaskNotFound):
            await self.core.rerun_failed("t-missing")


# ================================================================ 10. 分片
class ShardTests(TestCoreStackTestCase):
    """plan 型分片流程（§9.3）：plan job → shards 展开 → 屏障推进。"""

    async def test_plan_expands_shards_then_barrier_advances(self) -> None:
        await self.boot(caps=[_cap("node-a"), _cap("node-b")])
        task_id = await self.submit_spec(
            _spec(
                [_stage("unit", shard_mode="plan", params={"target": "x"}), _stage("report")],
                params={"env": "test"},
            )
        )
        self.assertEqual(await self.task_state(task_id), "planning")
        await self.tick()
        plan = self.job_messages()[0][1]
        self.assertEqual(plan["shard"], {"plan": True})

        # 回报分片清单（含 params 覆盖）
        await self.send_result(
            plan["job_id"],
            1,
            summary={
                "shards": [
                    {"unit_refs": ["tests/a", "tests/b"], "params": {"x": 1}},
                    {"unit_refs": ["tests/c"]},
                ]
            },
        )
        self.assertEqual(await self.task_state(task_id), "running")
        jobs = await self.jobs_of(task_id)
        self.assertEqual(len(jobs), 3)  # 1 plan + 2 分片
        shard_rows = sorted(
            (job for job in jobs if job["shard"] and not job["shard"].get("plan")),
            key=lambda job: job["shard"]["index"],
        )
        self.assertEqual(shard_rows[0]["shard"]["index"], 0)
        self.assertEqual(shard_rows[0]["shard"]["total"], 2)
        self.assertEqual(shard_rows[0]["shard"]["unit_refs"], ["tests/a", "tests/b"])
        self.assertEqual(shard_rows[0]["shard"]["params"], {"x": 1})
        self.assertEqual(shard_rows[1]["shard"]["unit_refs"], ["tests/c"])
        self.assertNotIn("params", shard_rows[1]["shard"])

        # 两个分片 job 全派发
        await self.tick()
        shard_sends = self.job_messages()[1:]
        self.assertEqual(len(shard_sends), 2)
        self.assertEqual(
            shard_sends[0][1]["shard"],
            {"index": 0, "total": 2, "unit_refs": ["tests/a", "tests/b"]},
        )
        self.assertNotIn("params", shard_sends[0][1]["shard"])
        # 参数合并：任务级 → Stage 级 → 分片 params 覆盖
        self.assertEqual(shard_sends[0][1]["params"], {"env": "test", "target": "x", "x": 1})
        self.assertEqual(shard_sends[1][1]["params"], {"env": "test", "target": "x"})

        # 全部回报后屏障推进下一 Stage
        for _node, descriptor in shard_sends:
            await self.send_result(
                descriptor["job_id"], descriptor["attempt"], summary={"passed": 1}
            )
        self.assertEqual(len(await self.jobs_of(task_id)), 4)
        await self.tick()
        stage2 = self.job_messages()[-1][1]
        self.assertEqual(stage2["stage_name"], "report")
        self.assertIsNone(stage2["shard"])
        await self.send_result(stage2["job_id"], stage2["attempt"])
        self.assertEqual(await self.task_state(task_id), "succeeded")
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"passed": 2})

    async def test_plan_without_shards_creates_single_job(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec([_stage("unit", shard_mode="plan")]))
        await self.tick()
        plan = self.job_messages()[0][1]
        await self.send_result(plan["job_id"], 1, summary={"total": 5})  # shards 缺失

        jobs = await self.jobs_of(task_id)
        self.assertEqual(len(jobs), 2)
        self.assertIsNone(jobs[1]["shard"])
        await self.tick()
        run = self.job_messages()[-1][1]
        self.assertIsNone(run["shard"])
        await self.send_result(run["job_id"], run["attempt"])
        self.assertEqual(await self.task_state(task_id), "succeeded")


# ================================================================ 11. Stage 组合与策略
class StagePolicyTests(TestCoreStackTestCase):
    """线性屏障、fail_fast / continue 失败策略（§5.2 推进规则）。"""

    async def test_linear_stages_advance_after_barrier(self) -> None:
        await self.boot(caps=[_cap("node-a"), _cap("node-b")])
        task_id = await self.submit_spec(_spec([_stage("unit"), _stage("report")]))
        await self.tick()
        self.assertEqual(len(self.job_messages()), 1)  # 只建首段 job
        self.assertEqual(len(await self.jobs_of(task_id)), 1)
        first = self.job_messages()[0][1]
        await self.send_result(first["job_id"], 1, summary={"passed": 1})
        self.assertEqual(len(await self.jobs_of(task_id)), 2)  # 屏障过后才建第二段
        await self.tick()
        second = self.job_messages()[1][1]
        self.assertEqual(second["stage_name"], "report")
        await self.send_result(second["job_id"], 1, summary={"passed": 2})
        self.assertEqual(await self.task_state(task_id), "succeeded")
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"passed": 3})  # 跨 Stage 数值求和

    async def test_fail_fast_stops_after_first_stage_failure(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(
            _spec([_stage("unit"), _stage("report")], failure_policy="fail_fast")
        )
        await self.tick()
        first = self.job_messages()[0][1]
        await self.send_result(first["job_id"], 1, status="failed", error="boom")
        self.assertEqual(await self.task_state(task_id), "failed")
        self.assertEqual(len(await self.jobs_of(task_id)), 1)  # 第二 Stage 不启动
        history = await self.history_rows(task_id)
        failed_rows = [row for row in history if row.to_state == "failed"]
        self.assertEqual(len(failed_rows), 1)
        self.assertEqual(failed_rows[0].reason, "stage unit failed")

    async def test_continue_runs_all_stages_then_failed(self) -> None:
        await self.boot(caps=[_cap("node-a"), _cap("node-b")])
        task_id = await self.submit_spec(
            _spec([_stage("unit"), _stage("report")], failure_policy="continue")
        )
        await self.tick()
        first = self.job_messages()[0][1]
        await self.send_result(first["job_id"], 1, status="failed", summary={"failed": 1})
        # continue：第二段照跑
        self.assertEqual(len(await self.jobs_of(task_id)), 2)
        await self.tick()
        second = self.job_messages()[1][1]
        await self.send_result(second["job_id"], 1, summary={"passed": 4})

        self.assertEqual(await self.task_state(task_id), "failed")
        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"failed": 1, "passed": 4})  # 两段都聚合
        done = self.events["test/task-finished"]
        self.assertEqual(done[-1]["state"], "failed")


# ================================================================ 12. 查询面
class QueryTests(TestCoreStackTestCase):
    """get_task / list_tasks / list_jobs / get_result / cases / artifacts（§6）。"""

    async def test_list_tasks_filters_and_pagination(self) -> None:
        await self.boot()
        t1 = await self.submit_spec(_spec(name="t1", labels={"project": "p1", "env": "ci"}))
        t2 = await self.submit_spec(_spec(name="t2", labels={"project": "p2"}))
        self.assertEqual(
            {row["task_id"] for row in await self.core.list_tasks(labels={"project": "p1"})},
            {t1},
        )
        self.assertEqual(
            {row["task_id"] for row in await self.core.list_tasks(labels={"env": "ci"})},
            {t1},
        )
        self.assertEqual(len(await self.core.list_tasks(state="running")), 2)
        self.assertEqual(await self.core.list_tasks(state="succeeded"), [])
        self.assertEqual(
            [row["task_id"] for row in await self.core.list_tasks(limit=1, offset=1)],
            [t2],
        )
        self.assertIsNone(await self.core.get_task("t-missing"))

    async def test_jobs_result_cases_and_artifacts(self) -> None:
        await self.boot()
        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        await self.send_result(
            descriptor["job_id"],
            1,
            summary={"total": 2, "passed": 1, "failed": 1},
            cases=[_case("c1", "passed"), _case("c2", "failed", message="m")],
            artifacts=[_artifact("art-1")],
        )

        jobs = await self.jobs_of(task_id)
        self.assertEqual(jobs[0]["job_id"], descriptor["job_id"])
        self.assertEqual(len(jobs[0]["results"]), 1)

        result = await self.core.get_result(task_id)
        assert result is not None
        self.assertEqual(result["counts"], {"total": 2, "passed": 1, "failed": 1})
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["node_id"], "node-a")

        failed_only = await self.core.list_case_results(task_id=task_id, status="failed")
        self.assertEqual([row["case_id"] for row in failed_only], ["c2"])
        by_job = await self.core.list_case_results(job_id=descriptor["job_id"])
        self.assertEqual(len(by_job), 2)

        artifacts = await self.core.list_artifacts(task_id=task_id)
        self.assertEqual(artifacts[0]["artifact_id"], "art-1")
        self.assertTrue(
            await self.core.mark_artifact("art-1", uri="oss://bucket/run.log", state="uploaded")
        )
        uploaded = await self.core.list_artifacts(task_id=task_id, state="uploaded")
        self.assertEqual(uploaded[0]["uri"], "oss://bucket/run.log")
        self.assertEqual(await self.core.list_artifacts(task_id=task_id, state="local"), [])
        self.assertFalse(await self.core.mark_artifact("a-missing", state="removed"))


# ================================================================ 13. health_metrics
class HealthMetricsTests(TestCoreStackTestCase):
    """/healthz 指标：键齐全且计数随行为增长（DESIGN §14）。"""

    async def test_metric_keys_and_growth(self) -> None:
        await self.boot(config={"lease_timeout": 60.0, "max_attempts": 2})
        metrics = self.core.health_metrics()
        for key in (
            "queue_depth",
            "lease_timeouts_total",
            "retries_total",
            "inflight_jobs",
            "running_tasks",
            "results_duplicate_total",
            "late_results_total",
            "hooks",
        ):
            self.assertIn(key, metrics)
        self.assertEqual(
            set(metrics["hooks"]),
            {CHAIN_TASK_TRANSFORM, CHAIN_RESULT_TRANSFORM, CHAIN_ARTIFACT_SINK},
        )

        task_id = await self.submit_spec(_spec())
        await self.tick()
        _node, descriptor = self.job_messages()[0]
        job_id = descriptor["job_id"]

        # 租约超时 → lost 重派（lease_timeouts / retries 各 +1）
        self.clock.advance(61.0)
        await self.sweep()
        metrics = self.core.health_metrics()
        self.assertEqual(metrics["lease_timeouts_total"], 1)
        self.assertEqual(metrics["retries_total"], 1)

        # 迟到结果 → late_results_total +1
        await self.send_result(job_id, 1, summary={"passed": 1})
        self.assertEqual(self.core.health_metrics()["late_results_total"], 1)

        # 重复结果 → results_duplicate_total +1
        await self.tick()
        payload = _result_payload(job_id, 2, summary={"passed": 1})
        await self.core._on_job_result(payload, None)
        await self.core._on_job_result(payload, None)
        self.assertEqual(self.core.health_metrics()["results_duplicate_total"], 1)

        await self.core._refresh_gauges()
        metrics = self.core.health_metrics()
        self.assertEqual(metrics["running_tasks"], 0)  # 任务已终结
        self.assertEqual(metrics["inflight_jobs"], 0)
        self.assertEqual(await self.task_state(task_id), "succeeded")


if __name__ == "__main__":
    unittest.main()
