"""flow 远程执行（Phase 4 / §5.11）测试：多分片 / 去重 / 失联换节点 / unknown
打断 / 失败重试 / 无节点 / 载荷超限 / 能力发布（§7.5 故障注入：丢 result、
重复投递、从节点假死）。

进程内双节点 + comm 桩总线：master / worker 各一个 Context、各一份
FlowService；桩只做「按节点名投递 + 可注入丢包 / 重复 / 黑洞」，协议与
调度语义全部走真实 :class:`~common.plugins.flow.remote` 代码。
"""

from __future__ import annotations

import asyncio
import inspect
import time
import unittest
from contextlib import suppress
from typing import Any

from cordis_port import Context

from common.capabilities import CAP_EVENT
from common.cordis_utils import unload_all
from common.flow_contracts import (
    FlowDef,
    NodeDef,
    RemoteJobState,
    RemoteResult,
    RunState,
    StepContext,
    StepOutput,
    StepRetryable,
)
from common.plugins.flow import FlowService

#: [flow] 快速档（租约 / 置放 / 收尾都压小，测试秒级跑完）
FAST_FLOW: dict[str, object] = {
    "cancel_grace": 0.1,
    "retry_cap": 0.01,
    "step_timeout": 5,
    "resolve_wait": 0.5,
    "resume_delay": 0,
    "lease_interval": 0.05,
    "lease_timeout": 0.3,
    "place_wait": 0.5,
    "max_remote_payload_bytes": 4096,
}


class FakeBus:
    """进程内 comm 总线：按节点名路由；可注入丢包 / 重复 / 出向黑洞。"""

    def __init__(self) -> None:
        self.members: dict[str, "FakeComm"] = {}
        self.hub = "master"
        self.blackholed: set[str] = set()  # 出向黑洞（收得到、发不出）
        self.drop_from: set[tuple[str, str]] = set()  # (发送方, topic) 丢一次
        self.duplicate: set[str] = set()  # topic → 投递两遍
        self._tasks: set[asyncio.Task[None]] = set()

    def send(self, sender: str, target: str, topic: str, payload: object) -> None:
        if sender in self.blackholed:
            return
        if (sender, topic) in self.drop_from:
            self.drop_from.discard((sender, topic))
            return
        times = 2 if topic in self.duplicate else 1
        self.duplicate.discard(topic)
        peer = self.members.get(target)
        if peer is None:
            return
        for _ in range(times):
            task = asyncio.create_task(peer._deliver(sender, topic, payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


class FakeComm:
    """comm 服务桩（结构同 ``CommInterface`` 的发送 / 订阅子集）。"""

    def __init__(self, node: str, bus: FakeBus) -> None:
        self.node = node
        self._bus = bus
        self._handlers: dict[str, list[Any]] = {}
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        bus.members[node] = self

    def on(self, topic: str, handler: Any) -> Any:
        bucket = self._handlers.setdefault(topic, [])
        bucket.append(handler)

        def dispose() -> None:
            with suppress(ValueError):
                bucket.remove(handler)

        return dispose

    def send_to(
        self, node: str, topic: str, payload: Any,
        *, policy: Any = None, ttl: float | None = None, immediate: bool = False,
    ) -> str:
        self.sent.append((node, topic, dict(payload)))
        self._bus.send(self.node, node, topic, payload)
        return f"m-{len(self.sent)}"

    def report(
        self, topic: str, payload: Any,
        *, policy: Any = None, ttl: float | None = None, immediate: bool = False,
    ) -> str:
        self.sent.append((self._bus.hub, topic, dict(payload)))
        self._bus.send(self.node, self._bus.hub, topic, payload)
        return f"m-{len(self.sent)}"

    async def _deliver(self, sender: str, topic: str, payload: Any) -> None:
        meta: dict[str, Any] = {
            "node": sender, "topic": topic,
            "ts": time.time(), "received_ts": time.time(), "id": "",
        }
        for handler in list(self._handlers.get(topic, [])):
            result = handler(payload, meta)
            if inspect.isawaitable(result):
                await result


class FakeNodes:
    """``NodesInterface`` 桩：能力标签子集匹配（§5.11.2 选节点口径）。"""

    def __init__(self, caps: list[dict[str, Any]]) -> None:
        self._caps = caps

    def list(self, *, online_only: bool = False) -> list[dict[str, Any]]:
        return [
            {"node_id": cap["node_id"], "node_kind": "slave", "address": "", "status": "online"}
            for cap in self._caps
        ]

    def select(
        self,
        *,
        kinds: Any = None,
        labels: Any = None,
        online_only: bool = True,
    ) -> list[dict[str, Any]]:
        picked: list[dict[str, Any]] = []
        for cap in self._caps:
            if kinds is not None and cap.get("kind") not in kinds:
                continue
            cap_labels = cap.get("labels") or {}
            if labels and any(cap_labels.get(k) != v for k, v in labels.items()):
                continue
            picked.append(dict(cap))
        return picked


def _cap(
    node_id: str, stage: str, step_id: str, *,
    idempotent: bool = False, max_concurrency: int = 4, labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id, "kind": "flow.step", "id": f"{stage}/{step_id}",
        "provider": "test", "labels": {"stage": stage, **(labels or {})},
        "detail": {"idempotent": idempotent, "max_concurrency": max_concurrency},
    }


class FlowRemoteTests(unittest.IsolatedAsyncioTestCase):
    """远程作业机制（每个用例独立装配 master + worker）。"""

    async def asyncSetUp(self) -> None:
        self.bus = FakeBus()
        self.caps: list[dict[str, Any]] = []
        self.extra: list[Context] = []
        self.master = await self._boot("master")
        self.worker = await self._boot("worker-1")

    async def asyncTearDown(self) -> None:
        for ctx in [self.master, self.worker, *self.extra]:
            await unload_all(ctx)

    async def _boot(self, name: str) -> Context:
        ctx = Context()
        ctx.provide("node_kind", "master" if name == "master" else "slave")
        ctx.provide("node_name", name)
        ctx.provide("appconfig", None)
        ctx.provide("db", None)
        ctx.provide("comm", FakeComm(name, self.bus))
        await ctx.plugin(FlowService, dict(FAST_FLOW))
        return ctx

    async def _boot_worker(self, name: str) -> Context:
        ctx = await self._boot(name)
        self.extra.append(ctx)
        return ctx

    def install_nodes(self) -> FakeNodes:
        nodes = FakeNodes(self.caps)
        self.master.provide("nodes", nodes)
        return nodes

    def echo_handler(
        self, calls: list[str], *, fail: BaseException | None = None,
        gate: asyncio.Event | None = None,
    ):
        async def handler(sc: StepContext) -> StepOutput:
            calls.append(str(sc.params.get("shard")))
            if gate is not None:
                await gate.wait()
            if fail is not None:
                raise fail
            return {"shard": sc.params.get("shard")}

        return handler

    def dispatch_flow(self, flow_id: str, runner) -> None:
        """注册「dispatch 节点 + 远程收集」流程；``runner(sc)`` 返回结果列表。"""

        async def dispatch(sc: StepContext) -> StepOutput:
            results: list[RemoteResult] = await runner(sc)
            return {
                "states": [r.state.value for r in results],
                "nodes": [r.node for r in results],
                "attempts": [r.attempts for r in results],
            }

        self.master.flow.define(
            FlowDef(id=flow_id, version=1, nodes=(NodeDef("d", "dispatch"),))
        )
        self.master.flow.step("dispatch", "dispatch", dispatch)

    # --------------------------------------------------------- 多分片并行
    async def test_multi_shard_and_labels_select(self) -> None:
        """多分片并行：where 标签筛选、结果按序返回、run 全链路成功。"""
        seen: list[str] = []
        self.caps = [
            _cap("worker-1", "execute", "pytest", idempotent=True, labels={"framework": "pytest"}),
            _cap("worker-1", "execute", "jest", idempotent=True, labels={"framework": "jest"}),
        ]
        self.install_nodes()
        self.worker.flow.step("execute", "pytest", self.echo_handler(seen), idempotent=True)

        async def runner(sc: StepContext) -> list[RemoteResult]:
            return await self.master.flow.run_remote(
                sc, stage="execute", where={"framework": "pytest"},
                jobs=[{"shard": i} for i in range(3)],
                max_parallel=3, retries=1, timeout=10,
            )

        self.dispatch_flow("m1", runner)
        handle = await self.master.flow.start("m1")
        self.assertEqual(await handle.wait(15), RunState.SUCCEEDED)
        self.assertEqual(sorted(seen), ["0", "1", "2"])
        status = await self.master.flow.status(handle.id)
        assert status is not None
        self.assertEqual(status["nodes"][0]["state"], "succeeded")

    # --------------------------------------------------------- 重复投递去重
    async def test_duplicate_delivery_dedup(self) -> None:
        """重复投递：worker 同键同 nonce 只执行一次、结果重发；master 幂等收。"""
        seen: list[str] = []
        self.caps = [_cap("worker-1", "execute", "echo", idempotent=True)]
        self.install_nodes()
        self.worker.flow.step("execute", "echo", self.echo_handler(seen), idempotent=True)
        self.bus.duplicate.add("flow.step.run")
        self.bus.duplicate.add("flow.step.result")

        async def runner(sc: StepContext) -> list[RemoteResult]:
            return await self.master.flow.run_remote(
                sc, stage="execute", jobs=[{"shard": 7}], retries=0, timeout=10,
            )

        self.dispatch_flow("m2", runner)
        handle = await self.master.flow.start("m2")
        self.assertEqual(await handle.wait(15), RunState.SUCCEEDED)
        self.assertEqual(seen, ["7"])  # 重复 run 只执行一次

    # ------------------------------------------------- 失联换节点重跑（丢 result）
    async def test_lost_reroutes_to_other_node(self) -> None:
        """丢 result + worker 假死（出向黑洞）：幂等作业失联后换节点重跑。"""
        self.caps = [
            _cap("worker-1", "execute", "echo", idempotent=True),
            _cap("worker-2", "execute", "echo", idempotent=True),
        ]
        self.install_nodes()
        seen_a: list[str] = []
        seen_b: list[str] = []
        self.worker.flow.step("execute", "echo", self.echo_handler(seen_a), idempotent=True)
        worker2 = await self._boot_worker("worker-2")
        worker2.flow.step("execute", "echo", self.echo_handler(seen_b), idempotent=True)
        self.bus.blackholed.add("worker-1")  # 收到作业但音讯全无

        async def runner(sc: StepContext) -> list[RemoteResult]:
            return await self.master.flow.run_remote(
                sc, stage="execute", jobs=[{"shard": 1}], retries=1, timeout=10,
            )

        self.dispatch_flow("m3", runner)
        handle = await self.master.flow.start("m3")
        self.assertEqual(await handle.wait(15), RunState.SUCCEEDED)
        self.assertEqual(len(seen_a), 1)  # worker-1 执行过（结果丢了）
        self.assertEqual(seen_b, ["1"])  # 换节点重跑成功

    # ----------------------------------------------------- unknown 打断（假死）
    async def test_unknown_interrupts_run(self) -> None:
        """非幂等作业失联且无预算 → 结果 unknown、run interrupted、节点 unknown。"""
        self.caps = [_cap("worker-1", "execute", "echo", idempotent=False)]
        self.install_nodes()
        seen: list[str] = []
        self.worker.flow.step("execute", "echo", self.echo_handler(seen), idempotent=False)
        self.bus.blackholed.add("worker-1")

        async def runner(sc: StepContext) -> list[RemoteResult]:
            return await self.master.flow.run_remote(
                sc, stage="execute", jobs=[{"shard": 1}], retries=0, timeout=10,
            )

        self.master.flow.define(
            FlowDef(
                id="m4", version=1,
                nodes=(NodeDef("d", "dispatch"), NodeDef("tail", "tail", always=True)),
            )
        )
        self.master.flow.step("dispatch", "dispatch", self._dispatch_of(runner))
        tail: list[str] = []

        async def tail_step(_sc: StepContext) -> StepOutput:
            tail.append("tail")
            return None

        self.master.flow.step("tail", "tail", tail_step)
        events: list[dict[str, object]] = []
        self.master.on("flow/run-interrupted", lambda p: events.append(dict(p)))
        handle = await self.master.flow.start("m4")
        self.assertEqual(await handle.wait(15), RunState.INTERRUPTED)
        status = await self.master.flow.status(handle.id)
        assert status is not None
        self.assertEqual(status["state"], "interrupted")
        states = {node["node_id"]: node["state"] for node in status["nodes"]}
        self.assertEqual(states["d"], "unknown")  # 不臆断结果（§5.11.5）
        self.assertEqual(tail, [])  # 余下节点保持 pending（待 resume）
        self.assertEqual(len(events), 1)

    # --------------------------------------------------------- 失败重试（可重试）
    async def test_retryable_failure_reroutes(self) -> None:
        """可重试失败（StepRetryable）：有预算就换节点重试直至成功。"""
        self.caps = [
            _cap("worker-1", "execute", "echo", idempotent=False),
            _cap("worker-2", "execute", "echo", idempotent=False),
        ]
        self.install_nodes()
        seen_a: list[str] = []
        seen_b: list[str] = []
        self.worker.flow.step(
            "execute", "echo", self.echo_handler(seen_a, fail=StepRetryable("boom")),
            idempotent=False,
        )
        worker2 = await self._boot_worker("worker-2")
        worker2.flow.step("execute", "echo", self.echo_handler(seen_b), idempotent=False)

        async def runner(sc: StepContext) -> list[RemoteResult]:
            return await self.master.flow.run_remote(
                sc, stage="execute", jobs=[{"shard": 1}], retries=1, timeout=10,
            )

        self.dispatch_flow("m5", runner)
        handle = await self.master.flow.start("m5")
        self.assertEqual(await handle.wait(15), RunState.SUCCEEDED)
        self.assertEqual(len(seen_a), 1)
        self.assertEqual(seen_b, ["1"])

    # ------------------------------------------------------------- 无候选节点
    async def test_no_capable_node(self) -> None:
        """无候选节点：place_wait 后 failed(no_capable_node)。"""
        self.install_nodes()  # 空能力集
        captured: list[RemoteResult] = []

        async def runner(sc: StepContext) -> list[RemoteResult]:
            results = await self.master.flow.run_remote(
                sc, stage="execute", jobs=[{"shard": 1}], retries=0, timeout=10,
            )
            captured.extend(results)
            return results

        self.dispatch_flow("m6", runner)
        handle = await self.master.flow.start("m6")
        self.assertEqual(await handle.wait(15), RunState.SUCCEEDED)
        self.assertEqual([r.state for r in captured], [RemoteJobState.FAILED])
        self.assertEqual(captured[0].error, "no_capable_node")

    # ------------------------------------------------------------- 载荷超限
    async def test_payload_too_large(self) -> None:
        """载荷超限（max_remote_payload_bytes）：作业直接 failed(payload_too_large)。"""
        self.caps = [_cap("worker-1", "execute", "echo", idempotent=True)]
        self.install_nodes()
        seen: list[str] = []
        self.worker.flow.step("execute", "echo", self.echo_handler(seen), idempotent=True)
        captured: list[RemoteResult] = []

        async def runner(sc: StepContext) -> list[RemoteResult]:
            results = await self.master.flow.run_remote(
                sc, stage="execute", jobs=[{"blob": "x" * 9000}], retries=0, timeout=10,
            )
            captured.extend(results)
            return results

        self.dispatch_flow("m7", runner)
        handle = await self.master.flow.start("m7")
        self.assertEqual(await handle.wait(15), RunState.SUCCEEDED)
        self.assertEqual(seen, [])  # 从未下发
        self.assertEqual(captured[0].error, "payload_too_large")

    # --------------------------------------------- 主节点重启后恢复挂接（§5.11.6）
    async def test_master_restart_resumes_and_redispatches(self) -> None:
        """崩溃在途的 dispatch 节点：重启恢复重放 → 远程作业重新下发（§7.5 验收）。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from common.plugins.appconfig import AppConfigPlugin
        from common.plugins.db import DbPlugin
        from common.plugins.flow.store import FlowStore

        async def _noop(*_a: object, **_k: object) -> None:
            return None

        self.caps = [_cap("worker-1", "execute", "echo", idempotent=True)]
        self.install_nodes()
        seen: list[str] = []
        self.worker.flow.step("execute", "echo", self.echo_handler(seen), idempotent=True)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bus.hub = "master-r"  # 汇聚点跟真 master 走（桩 report 的目标）
        root = Path(temp.name)
        config_path = root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\nname = master-r\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(root / 'aetp.db').as_posix()}\n"
            "\n[flow]\n"
            "cancel_grace = 0.1\nretry_cap = 0.01\nstep_timeout = 5\n"
            "resolve_wait = 1\nlease_interval = 0.05\nlease_timeout = 0.3\n"
            "place_wait = 1\nresume_delay = 0\n",
            encoding="utf-8",
        )

        async def boot_master() -> Context:
            ctx = Context()
            ctx.provide("node_kind", "master")
            ctx.provide("node_name", "master-r")
            ctx.provide("comm", FakeComm("master", self.bus))
            ctx.provide("nodes", FakeNodes(self.caps))
            await ctx.plugin(AppConfigPlugin, {"path": str(config_path)})
            await ctx.plugin(DbPlugin)
            await ctx.plugin(FlowService)
            self.extra.append(ctx)
            return ctx

        def install_dispatch(ctx: Context) -> None:
            async def dispatch(sc: StepContext) -> StepOutput:
                await ctx.flow.run_remote(
                    sc, stage="execute", jobs=[{"shard": 1}], retries=0, timeout=10,
                )
                return None

            ctx.flow.define(FlowDef(id="mr", version=1, nodes=(NodeDef("d", "dispatch"),)))
            ctx.flow.step("dispatch", "dispatch", dispatch, idempotent=True)

        master1 = await boot_master()
        install_dispatch(master1)
        # 崩溃现场：attempt-started 落库、收尾记录全丢（同 §5.10 的失忆桩）
        with (
            patch.object(FlowStore, "record_attempt_finished", _noop),
            patch.object(FlowStore, "record_node_finished", _noop),
            patch.object(FlowStore, "record_run_finished", _noop),
        ):
            handle = await master1.flow.start("mr")
            self.assertEqual(await handle.wait(15), RunState.SUCCEEDED)
            run_id = handle.id
        self.assertEqual(seen, ["1"])  # 崩溃前执行过一次
        await unload_all(master1)

        master2 = await boot_master()  # 重启：恢复扫描自动重放
        install_dispatch(master2)
        for _ in range(100):
            status = await master2.flow.status(run_id)
            if status and status["state"] in ("succeeded", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        assert status is not None
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(seen, ["1", "1"])  # 恢复重放 → 远程作业重新下发

    # --------------------------------------------------------- 能力发布 / 依赖
    async def test_capability_publishing_and_missing_comm(self) -> None:
        """CAP_EVENT 发布 flow.step 信封（detail 带幂等 / 容量）；缺 comm 报错。"""

        async def handler(_sc: StepContext) -> StepOutput:
            return None

        self.master.flow.step("execute", "echo", handler, idempotent=True, max_concurrency=2)
        bucket: list[dict[str, object]] = []
        await self.master.parallel(CAP_EVENT, bucket)
        entries = [e for e in bucket if e.get("kind") == "flow.step"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "execute/echo")
        self.assertEqual(
            entries[0]["detail"],
            {"params_schema": None, "idempotent": True, "max_concurrency": 2},
        )

        bare = Context()
        bare.provide("node_kind", "slave")
        bare.provide("node_name", "bare")
        bare.provide("appconfig", None)
        bare.provide("db", None)
        await bare.plugin(FlowService, dict(FAST_FLOW))
        try:
            sc = StepContext(
                run_id="r", flow_id="f", node_id="n", attempt=1,
                params={}, input={}, data={}, cancelled=asyncio.Event(),
            )
            with self.assertRaisesRegex(Exception, "comm"):
                await bare.flow.run_remote(sc, stage="execute", jobs=[{}])
        finally:
            await unload_all(bare)

    # ------------------------------------------------------------- 辅助
    @staticmethod
    def _dispatch_of(runner):
        async def dispatch(sc: StepContext) -> StepOutput:
            await runner(sc)
            return None

        return dispatch


if __name__ == "__main__":
    unittest.main()
