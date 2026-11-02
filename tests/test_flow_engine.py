"""flow 引擎测试：注册 / 预检 / 执行 / 重试 / 取消 / 排空 / 并发 / 事件 / 覆盖
（设计 §7.5 必测 + 附录 C 参考流程语义）；装配含 db（Phase 3 持久化接线）。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context

from common.cordis_utils import unload_all
from common.flow_contracts import (
    FlowDef,
    FlowError,
    FlowRejected,
    JsonValue,
    Mode,
    NodeDef,
    Overrides,
    ProblemCode,
    ProblemLevel,
    RunState,
    SlotDef,
    StepContext,
    StepOutput,
    StepRetryable,
    StepSkipped,
)
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.db import DbPlugin
from common.plugins.flow import FlowService

#: 附录 C 的参考流程（Python 形态，与 C.2 的 JSON 同构）
TEST_TASK = FlowDef(
    id="test-task",
    version=1,
    title="测试任务",
    nodes=(
        NodeDef("split", "split", locked=True),
        NodeDef("dispatch", "dispatch", locked=True, timeout=3600),
        NodeDef("collect", "collect", locked=True),
        NodeDef("stats", "stats", locked=True),
        NodeDef("report", "report", labels={"format": "html"}, required=False),
        NodeDef("persist", "persist", always=True),
    ),
    slots=(SlotDef("after-stats", after="stats", stages=("audit", "notify", "report")),),
)


class FlowEngineTests(unittest.IsolatedAsyncioTestCase):
    """引擎核心语义（每个用例独立装配最小栈）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._root = Path(self._temp.name)
        config_path = self._root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\nname = test-node\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self._root / 'aetp.db').as_posix()}\n"
            "\n[flow]\n"
            "max_concurrent_runs = 4\n"
            "cancel_grace = 0.1\n"
            "retry_cap = 0.01\n"
            "step_timeout = 5\n",
            encoding="utf-8",
        )
        self.context: Context = Context()
        self.context.provide("node_kind", "master")
        self.context.provide("node_name", "test-node")
        self.events: list[tuple[str, dict[str, object]]] = []
        self.context.on(
            "flow/run-started", lambda p: self.events.append(("started", p))
        )
        self.context.on(
            "flow/node-finished", lambda p: self.events.append(("node", p))
        )
        self.context.on(
            "flow/run-finished", lambda p: self.events.append(("finished", p))
        )
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)  # flow 持久化地基（§5.10 事件日志）
        await self.context.plugin(FlowService)  # 配置走 [flow] 节（ini 注入）
        self.context.flow.define(TEST_TASK)

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        for _ in range(50):  # Windows：aiosqlite 后台线程关文件略有延迟
            try:
                self._temp.cleanup()
                break
            except PermissionError:
                await asyncio.sleep(0.05)

    # ------------------------------------------------------------- 辅助
    def register_defaults(
        self,
        *,
        collect_error: BaseException | None = None,
        skip: frozenset[str] = frozenset(),
    ) -> dict[str, list[str]]:
        """按附录 C 注册一套 dummy 提供者；返回各节点调用记录。"""
        calls: dict[str, list[str]] = {
            n: [] for n in ("split", "dispatch", "collect", "stats", "report", "persist")
        }

        async def split(sc: StepContext) -> StepOutput:
            calls["split"].append(sc.node_id)
            return {"shards": [0, 1]}

        async def dispatch(sc: StepContext) -> StepOutput:
            calls["dispatch"].append(sc.node_id)
            shards = cast(list[JsonValue], cast(dict[str, JsonValue], sc.data["split"])["shards"])
            return {"assigned": len(shards)}

        async def collect(sc: StepContext) -> StepOutput:
            calls["collect"].append(sc.node_id)
            if collect_error is not None:
                raise collect_error
            return {"results": 2}

        async def stats(sc: StepContext) -> StepOutput:
            calls["stats"].append(sc.node_id)
            return {"passed": 120}

        async def report(sc: StepContext) -> StepOutput:
            calls["report"].append(sc.node_id)
            return {"path": f"reports/{sc.run_id}.html"}

        async def persist(sc: StepContext) -> StepOutput:
            calls["persist"].append(sc.node_id)
            return {"saved": True}

        flow = self.context.flow
        providers = {
            "split": ("split", "by-case", split, {}),
            "dispatch": ("dispatch", "all-slaves", dispatch, {}),
            "collect": ("collect", "default", collect, {}),
            "stats": ("stats", "backfill", stats, {}),
            "report": ("report", "html", report, {"labels": {"format": "html"}}),
            "persist": ("persist", "db", persist, {}),
        }
        for name, (stage, step_id, handler, extra) in providers.items():
            if name not in skip:
                flow.step(stage, step_id, handler, idempotent=True, **extra)
        return calls

    def node_events(self) -> list[dict[str, object]]:
        return [p for name, p in self.events if name == "node"]

    def finished_event(self) -> dict[str, object]:
        for name, payload in reversed(self.events):
            if name == "finished":
                return payload
        raise AssertionError("没有 flow/run-finished")

    # ------------------------------------------------------- 附录 C 场景
    async def test_full_success(self) -> None:
        """全部成功 → succeeded；黑板逐级可见；事件齐全。"""
        calls = self.register_defaults()
        run = await self.context.flow.start("test-task", {"cases": ["a"]})
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)
        self.assertEqual(calls["split"], ["split"])
        self.assertEqual(calls["persist"], ["persist"])
        states = {e["node_id"]: e["state"] for e in self.node_events()}
        self.assertEqual(states["split"], "succeeded")
        self.assertEqual(states["persist"], "succeeded")
        self.assertEqual(states["report"], "succeeded")
        self.assertEqual(self.finished_event()["state"], "succeeded")
        started = [p for name, p in self.events if name == "started"]
        self.assertEqual(started[0]["flow_id"], "test-task")

    async def test_required_failure_runs_always(self) -> None:
        """collect 失败（必选）→ 后续跳过、persist（always）照常 → failed。"""
        calls = self.register_defaults(collect_error=ValueError("down"))
        run = await self.context.flow.start("test-task")
        self.assertEqual(await run.wait(10), RunState.FAILED)
        self.assertEqual(calls["stats"], [])  # prior_failure 跳过
        self.assertEqual(calls["report"], [])
        self.assertEqual(calls["persist"], ["persist"])  # always：失败也要保存
        states = {e["node_id"]: (e["state"], e.get("reason")) for e in self.node_events()}
        self.assertEqual(states["collect"][0], "failed")
        self.assertEqual(states["stats"], ("skipped", "prior_failure"))
        self.assertEqual(self.finished_event()["state"], "failed")

    async def test_optional_missing_provider_warns(self) -> None:
        """可选节点缺提供者 → 跳过记 warning，run 照常 succeeded（附录 C）。"""
        self.register_defaults(skip=frozenset({"report"}))
        problems = self.context.flow.preflight("test-task")
        self.assertFalse([p for p in problems if p.level is ProblemLevel.ERROR])
        self.assertTrue([p for p in problems if p.code is ProblemCode.NO_PROVIDER])
        run = await self.context.flow.start("test-task")
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)
        states = {e["node_id"]: e["state"] for e in self.node_events()}
        self.assertEqual(states["report"], "skipped")
        self.assertTrue(self.finished_event()["warnings"])

    async def test_required_missing_provider_rejected(self) -> None:
        """必选节点缺提供者 → 预检拒绝（消息列已注册步骤），不产生 run（附录 C）。"""
        self.register_defaults()
        # 注销必选的 stats
        problems = self.context.flow.preflight("test-task")
        self.assertEqual(problems, [])
        # 换个流程：必选节点指向未注册的 stage
        lonely = FlowDef(
            id="lonely", version=1,
            nodes=(NodeDef("need", "no-such-stage"),),
        )
        self.context.flow.define(lonely)
        problems = self.context.flow.preflight("lonely")
        errors = [p for p in problems if p.level is ProblemLevel.ERROR]
        self.assertEqual(errors[0].code, ProblemCode.NO_PROVIDER)
        self.assertIn("已注册", errors[0].message)  # 列出当前已注册的步骤
        with self.assertRaises(FlowRejected) as caught:
            await self.context.flow.start("lonely")
        self.assertTrue(caught.exception.problems)

    async def test_ambiguous_rejected_and_resolved_by_priority(self) -> None:
        """mode=one 候选并列 → 预检歧义；priority / pin 消歧。"""
        flow = self.context.flow
        base = FlowDef(
            id="one", version=1, nodes=(NodeDef("go", "go"),),
        )
        flow.define(base)

        async def a(_sc: StepContext) -> StepOutput:
            return {"who": "a"}

        async def b(_sc: StepContext) -> StepOutput:
            return {"who": "b"}

        flow.step("go", "a", a)
        flow.step("go", "b", b)
        problems = flow.preflight("one")
        self.assertTrue([p for p in problems if p.code is ProblemCode.AMBIGUOUS])
        with self.assertRaises(FlowRejected):
            await flow.start("one")

        # priority 消歧：高者胜
        flow.step("go", "a", a, priority=10)
        self.assertEqual(flow.preflight("one"), [])
        run = await flow.start("one")
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)

    async def test_overrides_disable_insert_and_locked(self) -> None:
        """覆盖：关可选 ✓ / 关锁定 ✗ / 插槽插入 ✓ / 非法插槽 ✗（§5.14.2）。"""
        self.register_defaults()
        flow = self.context.flow

        async def notify(sc: StepContext) -> StepOutput:
            return {"channel": "wecom"}

        flow.step("notify", "wecom", notify)
        overrides: Overrides = {
            "disable": ["report"],
            "insert": [
                {
                    "after": "stats",
                    "node": {"id": "notify", "stage": "notify", "params": {"channel": "wecom"}},
                }
            ],
        }
        self.assertEqual(flow.preflight("test-task", overrides), [])
        run = await flow.start("test-task", overrides=overrides)
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)
        states = {e["node_id"]: e["state"] for e in self.node_events()}
        self.assertEqual(states["report"], "skipped")  # disabled → skipped
        self.assertEqual(states["notify"], "succeeded")

        with_locked: Overrides = {"disable": ["split"]}
        problems = flow.preflight("test-task", with_locked)
        self.assertTrue([p for p in problems if p.code is ProblemCode.LOCKED])

        bad_slot: Overrides = {
            "insert": [{"after": "split", "node": {"id": "x", "stage": "notify"}}]
        }
        problems = flow.preflight("test-task", bad_slot)
        self.assertTrue([p for p in problems if p.code is ProblemCode.UNKNOWN_SLOT])

        missing: Overrides = {"params": {"nope": {"a": 1}}}
        problems = flow.preflight("test-task", missing)
        self.assertTrue([p for p in problems if p.code is ProblemCode.UNKNOWN_NODE])

    async def test_retry_policy(self) -> None:
        """重试：StepRetryable 重试；普通异常仅幂等重试（§5.8.4）。"""
        attempts: list[int] = []

        async def flaky(sc: StepContext) -> StepOutput:
            attempts.append(sc.attempt)
            if sc.attempt < 3:
                raise StepRetryable("对端暂不可用")
            return {"ok": True}

        calls = {"n": 0}

        async def boom(_sc: StepContext) -> StepOutput:
            calls["n"] += 1
            raise ValueError("fatal")

        flow = self.context.flow
        # 三个阶段互不相扰（同一 stage 多提供者会歧义）
        flow.define(FlowDef(id="r", version=1, nodes=(NodeDef("go", "retry-go", retries=3, backoff=0.01),)))
        flow.step("retry-go", "flaky", flaky)
        run = await flow.start("r")
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)
        self.assertEqual(attempts, [1, 2, 3])

        flow.define(FlowDef(id="r2", version=1, nodes=(NodeDef("go", "fatal-go", retries=3, backoff=0.01),)))
        flow.step("fatal-go", "boom", boom)  # 非幂等：普通异常不重试
        run = await flow.start("r2")
        self.assertEqual(await run.wait(10), RunState.FAILED)
        self.assertEqual(calls["n"], 1)

        flow.define(FlowDef(id="r3", version=1, nodes=(NodeDef("go", "idem-go", retries=1, backoff=0.01),)))
        flow.step("idem-go", "boom-idem", boom, idempotent=True)  # 幂等：重试
        run = await flow.start("r3")
        self.assertEqual(await run.wait(10), RunState.FAILED)
        self.assertEqual(calls["n"], 1 + 2)  # r2 一次 + r3 两次

    async def test_timeout_counts_as_retryable_failure(self) -> None:
        """超时：两段式收尾（信号 → grace → 强制取消）；TimeoutError 可重试。"""

        async def slow(sc: StepContext) -> StepOutput:
            await asyncio.sleep(30)
            return {"late": True}

        flow = self.context.flow
        flow.define(
            FlowDef(id="t", version=1, nodes=(NodeDef("go", "go", timeout=0.1, retries=1, backoff=0.01),))
        )
        flow.step("go", "slow", slow, idempotent=True)
        run = await flow.start("t")
        self.assertEqual(await run.wait(10), RunState.FAILED)
        self.assertEqual(run.state, RunState.FAILED)
        node = next(e for e in self.node_events() if e["node_id"] == "go")
        self.assertEqual(node["attempts"], 2)  # 超时可重试

    async def test_cancel_mid_run(self) -> None:
        """取消：在途尝试收尾、后续跳过、always 照常 → cancelled。"""
        gate = asyncio.Event()
        calls: list[str] = []

        async def hold(sc: StepContext) -> StepOutput:
            calls.append("hold")
            await gate.wait()
            return {"ok": True}

        async def after(_sc: StepContext) -> StepOutput:
            calls.append("after")
            return {}

        async def final(_sc: StepContext) -> StepOutput:
            calls.append("final")
            return {}

        flow = self.context.flow
        flow.define(
            FlowDef(
                id="c",
                version=1,
                nodes=(
                    NodeDef("hold", "hold"),
                    NodeDef("after", "after"),
                    NodeDef("final", "final", always=True),
                ),
            )
        )
        flow.step("hold", "hold", hold)
        flow.step("after", "after", after)
        flow.step("final", "final", final)
        run = await flow.start("c")
        await asyncio.sleep(0.05)  # 让 hold 进入在途
        self.assertTrue(await flow.cancel(run.id, "用户取消"))
        self.assertEqual(await run.wait(10), RunState.CANCELLED)
        self.assertEqual(calls, ["hold", "final"])  # after 被跳过、always 照常
        states = {e["node_id"]: e["state"] for e in self.node_events()}
        self.assertEqual(states["after"], "cancelled")

    async def test_drain_waits_for_inflight(self) -> None:
        """排空：先让在途尝试做完（§5.12）；close 后新 run 预检即拒。"""
        started = asyncio.Event()
        release = asyncio.Event()
        done: list[str] = []

        async def slow(sc: StepContext) -> StepOutput:
            started.set()
            await release.wait()
            done.append("done")
            return {"ok": True}

        flow = self.context.flow
        flow.define(FlowDef(id="d", version=1, nodes=(NodeDef("go", "go"),)))
        handle = flow.step("go", "slow", slow)
        run = await flow.start("d")
        await started.wait()
        drain_task = asyncio.create_task(handle.drain(timeout=5))
        await asyncio.sleep(0.05)
        self.assertFalse(drain_task.done())  # 还在等在途
        release.set()
        await drain_task
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)
        self.assertEqual(done, ["done"])
        self.assertFalse(handle.alive)  # 已排空
        handle.close()
        problems = flow.preflight("d")
        self.assertTrue([p for p in problems if p.code is ProblemCode.NO_PROVIDER])

    async def test_steps_changed_and_unregister(self) -> None:
        """注册 / 注销派发 flow/steps-changed；fiber 卸载自动摘除。"""
        changed: list[dict[str, object]] = []
        self.context.on("flow/steps-changed", lambda p: changed.append(p))

        async def noop(_sc: StepContext) -> StepOutput:
            return {}

        handle = self.context.flow.step("x", "noop", noop)
        await asyncio.sleep(0.05)
        self.assertTrue(any(p["action"] == "added" for p in changed))
        self.assertEqual(len(self.context.flow.catalog(stage="x")), 1)
        handle.close()
        await asyncio.sleep(0.05)
        self.assertTrue(any(p["action"] == "removed" for p in changed))
        self.assertEqual(self.context.flow.catalog(stage="x"), [])

    async def test_mode_all_runs_every_provider(self) -> None:
        """mode=all：依排序键全部执行，输出为列表。"""
        seen: list[str] = []

        async def one(_sc: StepContext) -> StepOutput:
            seen.append("one")
            return {"who": "one"}

        async def two(_sc: StepContext) -> StepOutput:
            seen.append("two")
            return {"who": "two"}

        async def sink(sc: StepContext) -> StepOutput:
            return {"count": len(cast(list[JsonValue], sc.data["go"]))}

        flow = self.context.flow
        flow.define(
            FlowDef(
                id="all",
                version=1,
                nodes=(NodeDef("go", "go", mode=Mode.ALL), NodeDef("sink", "sink")),
            )
        )
        flow.step("go", "one", one)
        flow.step("go", "two", two)
        flow.step("sink", "sink", sink)
        run = await flow.start("all")
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)
        self.assertEqual(seen, ["one", "two"])  # 排序键 (-priority, provider, step_id)

    async def test_max_concurrency_serializes(self) -> None:
        """max_concurrency=1：两个 run 共用提供者时串行（稀缺资源语义）。"""
        live = 0
        peak = 0

        async def limited(sc: StepContext) -> StepOutput:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.1)
            live -= 1
            return {}

        flow = self.context.flow
        flow.define(
            FlowDef(
                id="m1",
                version=1,
                nodes=(NodeDef("go", "go"),),
            )
        )
        flow.step("go", "limited", limited, max_concurrency=1)
        run1 = await flow.start("m1")
        run2 = await flow.start("m1")
        self.assertEqual(await run1.wait(10), RunState.SUCCEEDED)
        self.assertEqual(await run2.wait(10), RunState.SUCCEEDED)
        self.assertEqual(peak, 1)

    async def test_idempotency_key_returns_same_run(self) -> None:
        """同 idempotency_key 重复 start 返回已有 run（CI 重试语义）。"""
        self.register_defaults()
        first = await self.context.flow.start("test-task", idempotency_key="ci-1")
        second = await self.context.flow.start("test-task", idempotency_key="ci-1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(await second.wait(10), RunState.SUCCEEDED)

    async def test_step_skipped_and_bad_params(self) -> None:
        """StepSkipped 记跳过不算失败；params_schema 违例预检报 bad_params。"""

        async def skipper(_sc: StepContext) -> StepOutput:
            raise StepSkipped("本次无需执行")

        async def echo(_sc: StepContext) -> StepOutput:
            return {}

        flow = self.context.flow
        flow.define(
            FlowDef(
                id="s",
                version=1,
                nodes=(NodeDef("go", "param-go", required=False, params={"level": 5}),),
            )
        )
        flow.step(
            "param-go", "skipper", skipper,
            params_schema={"properties": {"level": {"type": "string", "maxLength": 2}}},
        )
        problems = flow.preflight("s")
        self.assertTrue([p for p in problems if p.code is ProblemCode.BAD_PARAMS])

        flow.define(
            FlowDef(id="s2", version=1, nodes=(NodeDef("go", "skip-go", required=False),))
        )
        flow.step("skip-go", "skipper2", skipper)
        run = await flow.start("s2")
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)  # 跳过不算失败
        states = {e["node_id"]: e["state"] for e in self.node_events()}
        self.assertEqual(states["go"], "skipped")

        flow.define(FlowDef(id="s3", version=1, nodes=(NodeDef("go2", "echo-go"),)))
        flow.step("echo-go", "echo", echo)
        run = await flow.start("s3")
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)

    async def test_slave_start_rejected(self) -> None:
        """从节点不编排：start 抛 FlowError（§5.2）。"""
        await unload_all(self.context)
        self.context = Context()
        self.context.provide("node_kind", "slave")
        self.context.provide("node_name", "worker-1")
        self.context.provide("appconfig", None)  # 流程引擎不直接读配置，桩即可
        self.context.provide("db", None)  # 持久化桩：无库仅内存（§5.10 降级路径）
        await self.context.plugin(FlowService)
        self.context.flow.define(TEST_TASK)
        with self.assertRaisesRegex(FlowError, "本节点不编排"):
            await self.context.flow.start("test-task")

    async def test_shared_provider_two_flows(self) -> None:
        """多流程共用提供者、运行互不泄漏（Q2）。"""
        seen: list[str] = []

        async def shared(sc: StepContext) -> StepOutput:
            seen.append(f"{sc.flow_id}:{sc.node_id}")
            return {"run": sc.run_id}

        flow = self.context.flow
        flow.define(FlowDef(id="f1", version=1, nodes=(NodeDef("go", "go"),)))
        flow.define(FlowDef(id="f2", version=1, nodes=(NodeDef("go", "go"),)))
        flow.step("go", "shared", shared)
        r1 = await flow.start("f1")
        r2 = await flow.start("f2")
        self.assertEqual(await r1.wait(10), RunState.SUCCEEDED)
        self.assertEqual(await r2.wait(10), RunState.SUCCEEDED)
        self.assertEqual(sorted(seen), ["f1:go", "f2:go"])

    async def test_step_conflict_replacement_and_async_listener(self) -> None:
        """同 provider 重注册 = 替换；不同 provider 抢 id → ValueError；
        async 监听器照常收到事件（parallel 派发，GAP-2 反例）。"""
        flow = self.context.flow

        async def v1(_sc: StepContext) -> StepOutput:
            return {"v": 1}

        async def v2(_sc: StepContext) -> StepOutput:
            return {"v": 2}

        flow.step("go", "x", v1)
        flow.step("go", "x", v2)  # 同 provider（root）：替换，不抛
        self.assertEqual(len(flow.catalog(stage="go")), 1)

        class OtherPlugin:
            name: ClassVar[str] = "other"
            inject: ClassVar[list[str]] = ["flow"]

            def __init__(self, ctx: Context, _config: object | None = None) -> None:
                self.ctx = ctx

            def init(self) -> object:
                self.reg = self.ctx.flow.step("go", "x", v2)

                def cleanup() -> None:
                    self.reg.close()

                return cleanup

        with self.assertRaisesRegex(ValueError, "抢注同 id"):
            await self.context.plugin(OtherPlugin)

        got: list[str] = []

        async def listener(payload: dict[str, object]) -> None:
            got.append(str(payload["state"]))

        self.context.on("flow/run-finished", listener)
        self.register_defaults()
        run = await flow.start("test-task")
        self.assertEqual(await run.wait(10), RunState.SUCCEEDED)
        self.assertEqual(got, ["succeeded"])  # async 监听器执行（parallel）


if __name__ == "__main__":
    unittest.main()
