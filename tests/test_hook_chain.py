"""HookChain 行为检查（§4.3 语义表逐行对应；原 H1 的 12 项）。"""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable

from common.hook_chain import HookChain, OnError

Problems = list[str]


def _chain(problems: Problems) -> HookChain[str, str]:
    return HookChain(("observe", "guard", "authorize", "inner"), on_problem=problems.append)


async def _terminal(ctx: str) -> str:
    return f"terminal({ctx})"


class HookChainOrderTests(unittest.IsolatedAsyncioTestCase):
    """顺序：由阶段 + order 决定，与注册顺序无关。"""

    async def test_order_independent_of_registration(self) -> None:
        chain: HookChain[str, str] = _chain([])
        trace: list[str] = []

        def hook(label: str, phase: str, order: int) -> Callable[..., object]:
            async def run(ctx: str, nxt: object) -> str:
                trace.append(label)
                return await nxt()  # type: ignore[operator]

            chain.use(run, phase=phase, order=order, owner="o", name=label)
            return run

        hook("inner-first", "inner", 10)  # 逆序注册
        hook("observe-late", "observe", 200)
        hook("observe-early", "observe", 10)
        result = await chain.run("x", _terminal)
        self.assertEqual(result, "terminal(x)")
        self.assertEqual(trace, ["observe-early", "observe-late", "inner-first"])

    async def test_unknown_phase_rejected(self) -> None:
        chain: HookChain[str, str] = _chain([])

        async def run(ctx: str, nxt: object) -> str:
            return await nxt()  # type: ignore[operator]

        with self.assertRaisesRegex(ValueError, "未知阶段"):
            chain.use(run, phase="nope", owner="o")


class HookChainSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """短路 / 失败策略 / 取消的逐格语义。"""

    async def test_short_circuit_skips_inner(self) -> None:
        chain: HookChain[str, str] = _chain([])
        trace: list[str] = []

        async def outer(ctx: str, nxt: object) -> str:
            trace.append("outer")
            return await nxt()  # type: ignore[operator]

        async def reject(ctx: str, nxt: object) -> str:
            trace.append("reject")
            return "denied"  # 不调 next：短路

        async def inner(ctx: str, nxt: object) -> str:
            trace.append("inner")
            return await nxt()  # type: ignore[operator]

        chain.use(outer, phase="observe", owner="a", name="outer")
        chain.use(reject, phase="guard", owner="a", name="reject")
        chain.use(inner, phase="inner", owner="a", name="inner")

        async def terminal(ctx: str) -> str:
            trace.append("terminal")
            return "nope"

        result = await chain.run("x", terminal)
        self.assertEqual(result, "denied")  # 外层拦截器仍能看到短路返回值
        self.assertEqual(trace, ["outer", "reject"])

    async def test_next_twice_raises(self) -> None:
        chain: HookChain[str, str] = _chain([])

        async def bad(ctx: str, nxt: object) -> str:
            first = await nxt()  # type: ignore[operator]
            await nxt()  # type: ignore[operator]
            return first

        chain.use(bad, phase="observe", owner="a", name="bad")
        with self.assertRaisesRegex(RuntimeError, "next\\(\\) called multiple times"):
            await chain.run("x", _terminal)

    async def test_skip_error_before_next_continues(self) -> None:
        problems: Problems = []
        chain: HookChain[str, str] = _chain(problems)
        trace: list[str] = []

        async def broken(ctx: str, nxt: object) -> str:
            raise ValueError("boom-before")

        async def follower(ctx: str, nxt: object) -> str:
            trace.append("follower")
            return await nxt()  # type: ignore[operator]

        chain.use(broken, phase="guard", owner="a", name="broken", on_error=OnError.SKIP)
        chain.use(follower, phase="authorize", owner="a", name="follower")
        result = await chain.run("x", _terminal)
        self.assertEqual(result, "terminal(x)")  # 当它不存在，继续往下走
        self.assertEqual(trace, ["follower"])
        self.assertEqual(len(problems), 1)
        self.assertIn("boom-before", problems[0])

    async def test_skip_error_after_next_keeps_result(self) -> None:
        chain: HookChain[str, str] = _chain([])
        trace: list[str] = []

        async def noisy(ctx: str, nxt: object) -> str:
            await nxt()  # type: ignore[operator] 下游已完成
            raise ValueError("boom-after")  # 结果由链保留（frame.result）

        async def follower(ctx: str, nxt: object) -> str:
            trace.append("follower")
            return await nxt()  # type: ignore[operator]

        chain.use(noisy, phase="observe", owner="a", name="noisy", on_error=OnError.SKIP)
        chain.use(follower, phase="inner", owner="a", name="follower")
        result = await chain.run("x", _terminal)
        self.assertEqual(result, "terminal(x)")
        self.assertEqual(trace, ["follower"])

    async def test_downstream_error_propagates_not_counted(self) -> None:
        problems: Problems = []
        chain: HookChain[str, str] = _chain(problems)

        async def skipper(ctx: str, nxt: object) -> str:
            return await nxt()  # type: ignore[operator]

        chain.use(skipper, phase="observe", owner="a", name="skipper", on_error=OnError.SKIP)

        async def terminal(ctx: str) -> str:
            raise KeyError("downstream")

        with self.assertRaises(KeyError):
            await chain.run("x", terminal)
        self.assertEqual(problems, [])  # 下游的错不被 SKIP 吞、也不上报为本拦截器问题
        entry = chain.stats()[0]
        self.assertEqual(entry["errors"], 0)  # 不计入该拦截器的错误

    async def test_fail_error_propagates_without_running_handler(self) -> None:
        chain: HookChain[str, str] = _chain([])
        reached: list[str] = []

        async def deny(ctx: str, nxt: object) -> str:
            raise PermissionError("denied")

        async def follower(ctx: str, nxt: object) -> str:
            reached.append("follower")
            return await nxt()  # type: ignore[operator]

        chain.use(deny, phase="authorize", owner="a", name="deny", on_error=OnError.FAIL)
        chain.use(follower, phase="inner", owner="a", name="follower")

        async def terminal(ctx: str) -> str:
            reached.append("terminal")
            return "nope"

        with self.assertRaises(PermissionError):
            await chain.run("x", terminal)
        self.assertEqual(reached, [])  # FAIL：handler 不会被执行
        self.assertEqual(chain.stats()[0]["errors"], 1)

    async def test_cancelled_error_propagates(self) -> None:
        problems: Problems = []
        chain: HookChain[str, str] = _chain(problems)

        async def cancelled(ctx: str, nxt: object) -> str:
            raise asyncio.CancelledError()

        chain.use(cancelled, phase="observe", owner="a", name="cancelled", on_error=OnError.SKIP)
        with self.assertRaises(asyncio.CancelledError):
            await chain.run("x", _terminal)
        self.assertEqual(problems, [])  # 取消永远向上传播，不当可跳过的失败


class HookChainLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """快照语义与统计。"""

    async def test_dispose_takes_snapshot(self) -> None:
        chain: HookChain[str, str] = _chain([])
        gate = asyncio.Event()
        entered = asyncio.Event()
        trace: list[str] = []

        async def slow(ctx: str, nxt: object) -> str:
            entered.set()
            await gate.wait()
            return await nxt()  # type: ignore[operator]

        async def doomed(ctx: str, nxt: object) -> str:
            trace.append("doomed")
            return await nxt()  # type: ignore[operator]

        chain.use(slow, phase="observe", owner="a", name="slow")
        dispose = chain.use(doomed, phase="inner", owner="a", name="doomed")

        task = asyncio.create_task(chain.run("x", _terminal))
        await entered.wait()
        dispose()  # 运行中卸载
        gate.set()
        result = await task
        self.assertEqual(result, "terminal(x)")
        self.assertEqual(trace, ["doomed"])  # 在途请求走的是旧链快照

        trace.clear()
        result = await chain.run("y", _terminal)
        self.assertEqual(result, "terminal(y)")
        self.assertEqual(trace, [])  # 新请求看到新链

    async def test_self_time_excludes_downstream(self) -> None:
        chain: HookChain[str, str] = _chain([])

        async def watcher(ctx: str, nxt: object) -> str:
            return await nxt()  # type: ignore[operator]

        chain.use(watcher, phase="observe", owner="a", name="watcher")

        async def slow_terminal(ctx: str) -> str:
            await asyncio.sleep(0.05)
            return "done"

        self.assertEqual(await chain.run("x", slow_terminal), "done")
        entry = chain.stats()[0]
        self.assertEqual(entry["calls"], 1)
        self.assertEqual(entry["errors"], 0)
        self.assertLess(entry["self_ms"], 30.0)  # 不含下游 50ms

    async def test_describe_and_stats_sorted(self) -> None:
        chain: HookChain[str, str] = _chain([])

        async def b(ctx: str, nxt: object) -> str:
            return await nxt()  # type: ignore[operator]

        async def a(ctx: str, nxt: object) -> str:
            return await nxt()  # type: ignore[operator]

        chain.use(b, phase="inner", order=5, owner="o", name="b")
        chain.use(a, phase="observe", order=5, owner="o", name="a")
        self.assertEqual(
            [(info.phase, info.name) for info in chain.describe()],
            [("observe", "a"), ("inner", "b")],
        )
        self.assertEqual([row["name"] for row in chain.stats()], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
