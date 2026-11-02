"""flow 持久化（Phase 3 / §5.10）测试：事件日志 + 投影、重启恢复、幂等键、
保留清理（§7.5「落库 / 恢复 / 幂等键 / 保留清理」必测）。

崩溃现场用「记录器选择性失忆」模拟：attempt-started 照写、收尾记录丢弃，
数据库停在「尝试在途、无终态」——与真实进程崩溃后的落库形态一致。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cordis_port import Context

from common.cordis_utils import unload_all
from common.flow_contracts import (
    FlowDef,
    JsonValue,
    NodeDef,
    RunState,
    StepContext,
    StepOutput,
)
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.db import DbPlugin
from common.plugins.flow import FlowService
from common.plugins.flow.store import FlowStore


async def _noop(*_args: object, **_kwargs: object) -> None:
    """记录器失忆桩（模拟崩溃：收尾事件从未落库）。"""


def _flow(flow_id: str, *stages: str) -> FlowDef:
    return FlowDef(
        id=flow_id,
        version=1,
        nodes=tuple(
            NodeDef(f"n{index}", stage) for index, stage in enumerate(stages, 1)
        ),
    )


class FlowPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """落库 / 恢复 / 幂等键 / 保留清理（每个用例独立装配最小栈）。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._root = Path(self._temp.name)
        self.calls: list[str] = []
        await self.boot()

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        for _ in range(50):  # Windows：aiosqlite 后台线程关文件略有延迟
            try:
                self._temp.cleanup()
                break
            except PermissionError:
                await asyncio.sleep(0.05)

    # ------------------------------------------------------------- 装配
    async def boot(self, *, flow_ini: str = "") -> None:
        """装配最小栈（appconfig + db + flow）；``flow_ini`` 追加 [flow] 配置。"""
        config_path = self._root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\nname = test-node\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self._root / 'aetp.db').as_posix()}\n"
            "\n[flow]\n"
            "cancel_grace = 0.1\nretry_cap = 0.01\nstep_timeout = 5\n"
            "resolve_wait = 1\nresume_delay = 0\n" + flow_ini,
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_kind", "master")
        self.context.provide("node_name", "test-node")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(FlowService)

    def store(self) -> FlowStore:
        return FlowStore(self.context.db)

    def handler(self, name: str, output: dict[str, JsonValue] | None = None):
        async def run(_sc: StepContext) -> StepOutput:
            self.calls.append(name)
            return output if output is not None else {"ok": name}

        return run

    def gated_handler(self, name: str, gate: asyncio.Event):
        """闸门式 handler：记调用后挂在闸门上——用于制造「尝试在途」现场。"""

        async def run(_sc: StepContext) -> StepOutput:
            self.calls.append(name)
            await gate.wait()
            return {"ok": name}

        return run

    async def crash_inflight(self, run_id: str) -> None:
        """崩溃模拟：attempt-started 已落库、在途尝试直接随卸栈消失（收尾记录全丢）。"""
        for _ in range(100):
            if await self.store().steps(run_id):
                break
            await asyncio.sleep(0.02)
        else:
            self.fail("attempt-started 未落库")
        await unload_all(self.context)  # 在途取消：失忆桩丢弃一切收尾记录

    async def run_to_end(self, flow_id: str, **kwargs: object) -> str:
        handle = await self.context.flow.start(flow_id, **kwargs)  # type: ignore[arg-type]
        self.assertEqual(await handle.wait(10), RunState.SUCCEEDED)
        return handle.id

    # --------------------------------------------------------- 落库（事件 + 投影）
    async def test_run_lifecycle_persisted(self) -> None:
        """一次成功运行：事件序完整；runs / steps 投影同事务落库（§5.10）。"""
        flow = self.context.flow
        flow.define(_flow("p1", "alpha", "beta"))
        flow.step("alpha", "a", self.handler("a"))
        flow.step("beta", "b", self.handler("b"))
        run_id = await self.run_to_end("p1")

        events = await self.store().events(run_id)
        types = [event["type"] for event in events]
        self.assertEqual(types[0], "run-created")
        self.assertEqual(types[1], "run-started")
        self.assertIn("node-attempt-started", types)
        self.assertIn("node-attempt-finished", types)
        self.assertEqual(types.count("node-finished"), 2)
        self.assertEqual(types[-1], "run-finished")
        self.assertTrue(all(event["seq"] == index + 1 for index, event in enumerate(events)))

        row = await self.store().get_run(run_id)
        assert row is not None
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["idempotency_key"], "")
        self.assertIn("n1", row["bindings"])

        steps = await self.store().steps(run_id)
        self.assertEqual([step["node_id"] for step in steps], ["n1", "n2"])
        for step in steps:
            self.assertEqual(step["state"], "succeeded")
            self.assertEqual(step["attempt"], 1)
            self.assertTrue(step["output"])

        status = await self.context.flow.status(run_id)
        assert status is not None
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(len(status["nodes"]), 2)

    # --------------------------------------------------------------- 幂等键
    async def test_idempotency_key_survives_restart(self) -> None:
        """同幂等键跨重启去重：第二次 start 返回原 run（§5.10 幂等键唯一）。"""
        flow = self.context.flow
        flow.define(_flow("p2", "alpha"))
        flow.step("alpha", "a", self.handler("a"))
        handle = await flow.start("p2", idempotency_key="job-42")
        self.assertEqual(await handle.wait(10), RunState.SUCCEEDED)
        first_id = handle.id

        await unload_all(self.context)
        await self.boot()
        flow = self.context.flow
        flow.define(_flow("p2", "alpha"))
        flow.step("alpha", "a", self.handler("a2"))
        again = await flow.start("p2", idempotency_key="job-42")
        self.assertEqual(again.id, first_id)
        self.assertEqual(await again.wait(1), RunState.SUCCEEDED)
        rows, total = await self.store().list_runs()
        self.assertEqual(total, 1)
        self.assertEqual(len(rows), 1)

    # ------------------------------------------------------------- 恢复（重放）
    async def test_recovery_replays_idempotent_inflight(self) -> None:
        """幂等尝试崩溃在途 → 重启重放（attempt 序号接续，§5.10）。"""
        gate = asyncio.Event()
        flow = self.context.flow
        flow.define(_flow("p3", "alpha"))
        flow.step("alpha", "a", self.gated_handler("a", gate), idempotent=True)
        with (
            patch.object(FlowStore, "record_attempt_finished", _noop),
            patch.object(FlowStore, "record_node_finished", _noop),
            patch.object(FlowStore, "record_run_finished", _noop),
        ):
            handle = await flow.start("p3")
            run_id = handle.id
            await self.crash_inflight(run_id)

        await self.boot()
        steps = await self.store().steps(run_id)
        self.assertEqual(steps[0]["state"], "running")  # 崩溃现场：无收尾记录
        flow = self.context.flow
        flow.define(_flow("p3", "alpha"))
        flow.step("alpha", "a", self.handler("a-replay"), idempotent=True)
        status = await self._wait_terminal(run_id)
        self.assertEqual(status["state"], "succeeded")
        self.assertIn("a-replay", self.calls)  # 重放确实又跑了一次
        steps = await self.store().steps(run_id)
        self.assertEqual([step["attempt"] for step in steps], [1, 2])  # 序号接续
        self.assertEqual(steps[1]["state"], "succeeded")

    # --------------------------------------------------- 恢复（未知步打断）+ 处置
    async def test_recovery_interrupts_unknown_then_resume_retry(self) -> None:
        """非幂等尝试崩溃在途 → interrupted + unknown；resume(retry) 重放。"""
        gate = asyncio.Event()
        flow = self.context.flow
        flow.define(_flow("p4", "alpha"))
        flow.step("alpha", "a", self.gated_handler("a", gate))  # 非幂等
        with (
            patch.object(FlowStore, "record_attempt_finished", _noop),
            patch.object(FlowStore, "record_node_finished", _noop),
            patch.object(FlowStore, "record_run_finished", _noop),
        ):
            handle = await flow.start("p4")
            run_id = handle.id
            await self.crash_inflight(run_id)

        await self.boot()
        status = await self._wait_terminal(run_id, states=("interrupted",))
        self.assertEqual(status["state"], "interrupted")
        steps = await self.store().steps(run_id)
        self.assertEqual(steps[0]["state"], "unknown")  # 不臆断结果（§5.10）
        events = await self.store().events(run_id)
        self.assertIn("run-interrupted", [event["type"] for event in events])

        flow = self.context.flow
        flow.define(_flow("p4", "alpha"))
        flow.step("alpha", "a", self.handler("a-retry"))
        self.assertTrue(await flow.resume(run_id, "retry"))
        status = await self._wait_terminal(run_id)
        self.assertEqual(status["state"], "succeeded")
        self.assertIn("a-retry", self.calls)
        events = await self.store().events(run_id)
        self.assertIn("run-resume-requested", [event["type"] for event in events])

    async def test_resume_skip_and_abort(self) -> None:
        """resume(skip) 跳过未知步、补跑后续节点；resume(aback) 落 failed 终态。"""
        gate = asyncio.Event()
        flow = self.context.flow
        flow.define(_flow("p5", "alpha", "beta"))
        flow.step("alpha", "a", self.gated_handler("a", gate))
        flow.step("beta", "b", self.handler("b"))
        with (
            patch.object(FlowStore, "record_attempt_finished", _noop),
            patch.object(FlowStore, "record_node_finished", _noop),
            patch.object(FlowStore, "record_run_finished", _noop),
        ):
            handle = await flow.start("p5")
            skip_id = handle.id
            await self.crash_inflight(skip_id)  # n1 在途、n2 未及
        await self.boot()
        await self._wait_terminal(skip_id, states=("interrupted",))
        flow = self.context.flow
        flow.define(_flow("p5", "alpha", "beta"))
        flow.step("beta", "b", self.handler("b-skip"))
        self.assertTrue(await flow.resume(skip_id, "skip"))
        status = await self._wait_terminal(skip_id)
        self.assertEqual(status["state"], "succeeded")  # alpha skipped，beta 补跑
        self.assertIn("b-skip", self.calls)

        # abort：再造一个在途崩溃现场
        gate2 = asyncio.Event()
        flow.define(_flow("p6", "alpha"))
        flow.step("alpha", "a2", self.gated_handler("a2", gate2))
        with (
            patch.object(FlowStore, "record_attempt_finished", _noop),
            patch.object(FlowStore, "record_node_finished", _noop),
            patch.object(FlowStore, "record_run_finished", _noop),
        ):
            handle = await flow.start("p6")
            abort_id = handle.id
            await self.crash_inflight(abort_id)
        await self.boot()
        await self._wait_terminal(abort_id, states=("interrupted",))
        self.assertTrue(await self.context.flow.resume(abort_id, "abort"))
        row = await self.store().get_run(abort_id)
        assert row is not None
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["error"], "resume_abort")

    # --------------------------------------------------------- 恢复（补齐尾声）
    async def test_recovery_completes_missing_tail(self) -> None:
        """全部节点已记终态、run-finished 缺失：恢复只补终态，不重跑步骤。"""
        flow = self.context.flow
        flow.define(_flow("p7", "alpha"))
        flow.step("alpha", "a", self.handler("a"))
        with patch.object(FlowStore, "record_run_finished", _noop):
            run_id = await self.run_to_end("p7")
        self.assertEqual(self.calls, ["a"])

        await unload_all(self.context)
        await self.boot()
        status = await self._wait_terminal(run_id)
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(self.calls, ["a"])  # 没有重复执行
        events = await self.store().events(run_id)
        self.assertEqual([e["type"] for e in events].count("run-finished"), 1)

    # --------------------------------------------------------------- 保留清理
    async def test_retain_max_runs_cleanup(self) -> None:
        """保留双限（先到先删）：总数上限生效，事件 / 步骤连带清理（§5.10）。"""
        await unload_all(self.context)
        await self.boot(flow_ini="retain_days = 3650\nretain_max_runs = 2\n")
        flow = self.context.flow
        flow.define(_flow("p8", "alpha"))
        flow.step("alpha", "a", self.handler("a"))
        ids = [await self.run_to_end("p8") for _ in range(3)]
        oldest = ids[0]
        for _ in range(50):  # 清理在 run 收尾任务里，稍等生效
            rows, total = await self.store().list_runs()
            if total == 2:
                break
            await asyncio.sleep(0.05)
        rows, total = await self.store().list_runs()
        self.assertEqual(total, 2)
        self.assertNotIn(oldest, {row["id"] for row in rows})
        self.assertEqual(await self.store().events(oldest), [])
        self.assertEqual(await self.store().steps(oldest), [])

    # ------------------------------------------------------------- 辅助
    async def _wait_terminal(
        self, run_id: str, *, states: tuple[str, ...] = ("succeeded", "failed", "cancelled")
    ) -> dict[str, object]:
        for _ in range(100):
            status = await self.context.flow.status(run_id)
            assert status is not None
            if status["state"] in states:
                return status
            await asyncio.sleep(0.05)
        self.fail(f"run {run_id} 未到终态：{status['state']}")


if __name__ == "__main__":
    unittest.main()
