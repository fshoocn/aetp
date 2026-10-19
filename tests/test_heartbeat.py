"""心跳插件（slave_plugins/heartbeat）的单元 / 集成测试。

覆盖场景：主模块不在线、掉线、上线恢复、HTTP 错误、配置校验、自省接口、
插件卸载清理。
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError

from cordis_port import Context

from _helpers import free_port as _free_port
from _helpers import request as _request
from common.cordis_utils import unload_all, wait_until
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.webapi import WebApiPlugin
from slave_plugins.heartbeat import (
    CAP_EVENT,
    SEQ_MAX,
    STATUS_EVENT,
    Capability,
    HeartbeatConfig,
    HeartbeatMonitor,
    HeartbeatPlugin,
    HeartbeatSender,
    HeartbeatSendError,
    MasterState,
    capabilities_digest,
    capability,
    next_seq,
    parse_capability,
    select_capabilities,
)
from slave_plugins.heartbeat.capabilities import CapProblemAlerter, collect_capabilities
from slave_plugins.heartbeat.sender import parse_response_body


def _status_dict(base_url: str) -> dict[str, object]:
    """GET /api/heartbeat/status 并解析为字典。"""
    status, body = _request(base_url, "/api/heartbeat/status")
    if status != 200:
        raise RuntimeError(f"状态接口返回 {status}")
    parsed: object = json.loads(body)
    if not isinstance(parsed, dict):
        raise TypeError("状态响应不是对象")
    return cast(dict[str, object], parsed)


class HeartbeatConfigTests(unittest.TestCase):
    """[heartbeat] 配置节解析与校验。"""

    def test_from_ini_parses_and_defaults(self) -> None:
        config = HeartbeatConfig.from_ini({"master_url": "http://127.0.0.1:8080"})
        self.assertEqual(config.master_url, "http://127.0.0.1:8080")
        self.assertEqual(config.path, "/api/heartbeat")
        self.assertEqual(config.interval, 5.0)
        self.assertEqual(config.timeout, 3.0)
        self.assertEqual(config.max_backoff, 60.0)
        self.assertEqual(config.endpoint(), "http://127.0.0.1:8080/api/heartbeat")

    def test_endpoint_joins_path_without_double_slash(self) -> None:
        config = HeartbeatConfig.from_ini(
            {"master_url": "http://host:1/", "path": "/x"}
        )
        self.assertEqual(config.endpoint(), "http://host:1/x")

    def test_missing_master_url_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            HeartbeatConfig.from_ini({"interval": "5"})
        self.assertIn("master_url", str(caught.exception))

    def test_bad_scheme_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            HeartbeatConfig.from_ini({"master_url": "ftp://host"})
        self.assertIn("http", str(caught.exception))

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            HeartbeatConfig.from_ini({"master_url": "http://h", "bogus": "1"})
        self.assertIn("bogus", str(caught.exception))

    def test_non_numeric_interval_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            HeartbeatConfig.from_ini({"master_url": "http://h", "interval": "fast"})
        self.assertIn("interval", str(caught.exception))

    def test_non_positive_interval_raises(self) -> None:
        with self.assertRaises(ValueError) as caught:
            HeartbeatConfig.from_ini({"master_url": "http://h", "interval": "0"})
        self.assertIn("interval", str(caught.exception))

    def test_non_finite_interval_raises(self) -> None:
        """inf / nan 能被 float() 解析但会炸 sleep/settimeout，必须拒绝。"""
        for raw in ("inf", "nan", "1e400"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as caught:
                    HeartbeatConfig.from_ini(
                        {"master_url": "http://h", "interval": raw}
                    )
                self.assertIn("interval", str(caught.exception))

    def test_apply_server_skips_overflow_and_non_finite(self) -> None:
        """超大整数 / inf / nan 一律跳过且不抛异常（float(10**400) 会抛
        OverflowError，nan <= 0 恒 False 会溜过正数检查）。"""
        config = HeartbeatConfig(
            master_url="http://h", interval=5.0, timeout=3.0, max_backoff=60.0
        )
        applied, skipped = config.apply_server(
            {
                "interval": 10**400,
                "timeout": float("inf"),
                "max_backoff": float("nan"),
            }
        )
        self.assertEqual(applied, {})
        self.assertEqual(set(skipped), {"interval", "timeout", "max_backoff"})
        self.assertEqual(config.interval, 5.0)  # 原值未动
        self.assertEqual(config.timeout, 3.0)
        self.assertEqual(config.max_backoff, 60.0)


class HeartbeatSeqTests(unittest.TestCase):
    """心跳序号递增与封顶（不回绕）。"""

    def test_next_seq_increments(self) -> None:
        self.assertEqual(next_seq(0), 1)
        self.assertEqual(next_seq(41), 42)

    def test_next_seq_caps_without_wraparound(self) -> None:
        """到达 SEQ_MAX 后停在上限——回绕到 1 会被误判为从模块重启。"""
        self.assertEqual(next_seq(SEQ_MAX - 1), SEQ_MAX)
        self.assertEqual(next_seq(SEQ_MAX), SEQ_MAX)

    def test_seq_max_is_js_safe_integer(self) -> None:
        """上限取 JS 安全整数，主模块（浏览器侧）不会丢精度。"""
        self.assertEqual(SEQ_MAX, 2**53 - 1)


class CapabilityEnvelopeTests(unittest.TestCase):
    """能力信封：构造 / 解析 / 摘要 / 筛选。"""

    def test_capability_builder_fills_envelope(self) -> None:
        entry = capability(
            "executor",
            "pytest",
            "pytest-runner",
            version="8.3.4",
            labels={"lang": "python"},
            summary="Python 单测执行",
            detail={"cmd": "pytest -q"},
        )
        self.assertEqual(
            entry,
            {
                "kind": "executor",
                "id": "pytest",
                "provider": "pytest-runner",
                "version": "8.3.4",
                "labels": {"lang": "python"},
                "summary": "Python 单测执行",
                "detail": {"cmd": "pytest -q"},
            },
        )

    def test_capability_builder_omits_empty_optionals(self) -> None:
        entry = capability("executor", "pytest", "pytest-runner")
        self.assertEqual(
            entry, {"kind": "executor", "id": "pytest", "provider": "pytest-runner"}
        )

    def test_parse_capability_reads_envelope(self) -> None:
        cap = parse_capability(
            {
                "kind": "executor",
                "id": "pytest",
                "provider": "runner",
                "version": "8.3.4",
                "labels": {"lang": "python"},
                "summary": "单测",
                "detail": {"cmd": "pytest"},
            }
        )
        self.assertIsNotNone(cap)
        self.assertEqual(
            cast(Capability, cap),
            Capability(
                kind="executor",
                id="pytest",
                provider="runner",
                version="8.3.4",
                labels={"lang": "python"},
                summary="单测",
                detail={"cmd": "pytest"},
            ),
        )

    def test_parse_capability_rejects_missing_required(self) -> None:
        cases: list[object] = [
            "not-a-dict",
            {"id": "pytest", "provider": "r"},
            {"kind": "executor", "provider": "r"},
            {"kind": "executor", "id": "pytest"},
            {"kind": "", "id": "pytest", "provider": "r"},
            {"kind": "executor", "id": 1, "provider": "r"},
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_capability(raw))

    def test_parse_capability_degrades_bad_optionals(self) -> None:
        """可选字段类型不对降级为空，不整条丢弃。"""
        cap = parse_capability(
            {
                "kind": "executor",
                "id": "p",
                "provider": "r",
                "version": 5,
                "labels": "bad",
                "summary": None,
            }
        )
        self.assertIsNotNone(cap)
        typed = cast(Capability, cap)
        self.assertEqual(typed.version, "")
        self.assertEqual(typed.labels, {})
        self.assertEqual(typed.summary, "")

    def test_parse_capability_ignores_unknown_fields(self) -> None:
        cap = parse_capability(
            {"kind": "executor", "id": "p", "provider": "r", "future": 1}
        )
        self.assertIsNotNone(cap)
        self.assertEqual(cast(Capability, cap).id, "p")

    def test_digest_is_order_insensitive(self) -> None:
        first = capability("executor", "pytest", "runner")
        second = capability("driver", "serial", "board")
        self.assertEqual(
            capabilities_digest([first, second]),
            capabilities_digest([second, first]),
        )
        self.assertEqual(capabilities_digest([]), capabilities_digest([]))

    def test_digest_changes_with_content(self) -> None:
        base = capability("executor", "pytest", "runner")
        changed = capability("executor", "pytest", "runner", version="2")
        self.assertNotEqual(
            capabilities_digest([base]), capabilities_digest([changed])
        )

    def test_select_by_kind_labels_query(self) -> None:
        caps = [
            Capability(
                kind="executor",
                id="pytest",
                provider="a",
                summary="Python 单测",
                labels={"lang": "python"},
            ),
            Capability(
                kind="executor",
                id="jmeter",
                provider="b",
                summary="压测",
                labels={"lang": "java"},
            ),
            Capability(
                kind="driver", id="serial", provider="c", labels={"lang": "python"}
            ),
        ]
        def ids(picked: list[Capability]) -> list[str]:
            return [cap.id for cap in picked]

        self.assertEqual(ids(select_capabilities(caps)), ids(caps))  # 无条件=全量
        self.assertEqual(ids(select_capabilities(caps, kinds={"driver"})), ["serial"])
        self.assertEqual(
            ids(select_capabilities(caps, kinds={"executor", "driver"})),
            ["pytest", "jmeter", "serial"],
        )
        self.assertEqual(
            ids(select_capabilities(caps, labels={"lang": "python"})),
            ["pytest", "serial"],
        )
        self.assertEqual(ids(select_capabilities(caps, q="单测")), ["pytest"])
        self.assertEqual(
            ids(
                select_capabilities(
                    caps, kinds={"executor"}, labels={"lang": "python"}, q="pytest"
                )
            ),
            ["pytest"],
        )
        self.assertEqual(select_capabilities(caps, kinds={"none"}), [])


class CapabilityCollectTests(unittest.IsolatedAsyncioTestCase):
    """CAP_EVENT 收集：坏条目隔离、贡献者错误隔离、顺序稳定。"""

    async def test_collects_entries_and_sorts(self) -> None:
        """条目经规范序排序返回（摘要与贡献顺序无关）。"""

        def add_b(bucket: list[object]) -> None:
            bucket.append(capability("executor", "b", "p"))

        def add_a(bucket: list[object]) -> None:
            bucket.append(capability("executor", "a", "p"))

        ctx = Context()
        ctx.on(CAP_EVENT, add_b)
        ctx.on(CAP_EVENT, add_a)
        entries, problems = await collect_capabilities(ctx)
        self.assertEqual([entry["id"] for entry in entries], ["a", "b"])
        self.assertEqual(problems, [])

    async def test_bad_entries_are_dropped_and_reported(self) -> None:
        """缺必填 / 非对象 / 不可序列化的条目就地丢弃，好条目照常。"""

        def contribute(bucket: list[object]) -> None:
            bucket.append({"id": "x"})  # 缺 kind / provider
            bucket.append("not-a-dict")  # 不是对象
            bucket.append(capability("executor", "bad", "p", detail=object()))
            bucket.append(capability("executor", "ok", "p"))

        ctx = Context()
        ctx.on(CAP_EVENT, contribute)
        entries, problems = await collect_capabilities(ctx)
        self.assertEqual([entry["id"] for entry in entries], ["ok"])
        self.assertEqual(len(problems), 3)

    async def test_contributor_error_does_not_block_others(self) -> None:
        """某个贡献者抛错不影响其余贡献（错误进问题列表）。"""

        def add_ok(bucket: list[object]) -> None:
            bucket.append(capability("executor", "ok", "p"))

        def broken(bucket: list[object]) -> None:
            raise RuntimeError("贡献者坏了")

        ctx = Context()
        ctx.on(CAP_EVENT, add_ok)
        ctx.on(CAP_EVENT, broken)
        entries, problems = await collect_capabilities(ctx)
        self.assertEqual([entry["id"] for entry in entries], ["ok"])
        self.assertEqual(len(problems), 1)
        self.assertIn("贡献者坏了", problems[0])


class CapProblemAlarmTests(unittest.TestCase):
    """能力问题告警去重：同一问题连续出现只告警一次，消失一拍后重现重新告警。"""

    def test_realarms_after_problem_disappears(self) -> None:
        warnings: list[str] = []
        alerter = CapProblemAlerter(warnings.append)
        alerter.report(["问题A"])
        alerter.report(["问题A"])  # 连续出现不重复告警
        self.assertEqual(warnings, ["问题A"])
        alerter.report([])  # 消失一拍 → 复位
        alerter.report(["问题A"])  # 重现 → 再次告警
        self.assertEqual(warnings, ["问题A", "问题A"])


class _ScriptedSender:
    """按脚本逐次决定成功 / 失败的发送替身，用于状态机测试。

    脚本项是异常则抛出（模拟发送失败），是 dict 则作为主模块响应返回，
    其余（含脚本耗尽）返回空响应。
    """

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def send(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, dict):
            return dict(cast(dict[str, object], outcome))
        return {}


class HeartbeatMonitorTests(unittest.IsolatedAsyncioTestCase):
    """连接状态机：不在线 / 掉线 / 上线三态迁移与退避。"""

    def _config(self, *, max_backoff: float = 60.0) -> HeartbeatConfig:
        return HeartbeatConfig(
            master_url="http://127.0.0.1:8080",
            interval=0.01,
            timeout=0.5,
            max_backoff=max_backoff,
        )

    def _monitor(
        self, sender: _ScriptedSender, *, max_backoff: float = 60.0
    ) -> HeartbeatMonitor:
        return HeartbeatMonitor(
            self._config(max_backoff=max_backoff),
            sender.send,
            lambda: {"seq": len(sender.calls) + 1},
        )

    async def test_master_never_online_stays_offline(self) -> None:
        """主模块一直不在线：状态停在 OFFLINE，失败计数增长。"""
        sender = _ScriptedSender([URLError("refused"), URLError("refused")])
        monitor = self._monitor(sender)

        self.assertFalse(await monitor.beat_once())
        self.assertFalse(await monitor.beat_once())
        self.assertEqual(monitor.state, MasterState.OFFLINE)
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.failed, 2)
        self.assertEqual(snapshot.consecutive_failures, 2)
        self.assertEqual(snapshot.sent, 0)
        self.assertIn("refused", snapshot.last_error or "")

    async def test_online_then_drop_then_recover(self) -> None:
        """在线 → 掉线 → 恢复：状态迁移顺序正确，恢复后退避清零。"""
        outcomes: list[object] = [
            None,
            URLError("reset"),
            HeartbeatSendError("主模块返回错误状态 503"),
            None,
        ]
        sender = _ScriptedSender(outcomes)
        transitions: list[tuple[str, str]] = []
        config = HeartbeatConfig(
            master_url="http://127.0.0.1:8080",
            interval=0.01,
            timeout=0.5,
            max_backoff=60.0,
        )
        monitor = HeartbeatMonitor(
            config,
            sender.send,
            lambda: {"seq": 1},
            on_transition=lambda old, new, _err: transitions.append(
                (old.value, new.value)
            ),
        )

        self.assertTrue(await monitor.beat_once())  # 上线
        self.assertFalse(await monitor.beat_once())  # 掉线
        self.assertFalse(await monitor.beat_once())  # 持续失败不再迁移
        self.assertTrue(await monitor.beat_once())  # 恢复

        self.assertEqual(monitor.state, MasterState.ONLINE)
        self.assertEqual(
            transitions,
            [
                ("unknown", "online"),
                ("online", "offline"),
                ("offline", "online"),
            ],
        )
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.sent, 2)
        self.assertEqual(snapshot.consecutive_failures, 0)
        self.assertIsNone(snapshot.last_error)

    async def test_backoff_grows_and_caps(self) -> None:
        """失败退避按 2^n 增长并封顶在 max_backoff。"""
        sender = _ScriptedSender([URLError("x")] * 5)
        config = HeartbeatConfig(
            master_url="http://127.0.0.1:8080",
            interval=1.0,
            timeout=0.5,
            max_backoff=4.0,
        )
        monitor = HeartbeatMonitor(config, sender.send, lambda: {"seq": 1})

        delays: list[float] = []
        for _ in range(5):
            await monitor.beat_once()
            delays.append(monitor.next_delay())
        self.assertEqual(delays, [1.0, 2.0, 4.0, 4.0, 4.0])

    async def test_backoff_survives_thousands_of_failures(self) -> None:
        """上千次连续失败后退避仍在 max_backoff（指数不溢出炸循环）。"""
        sender = _ScriptedSender([URLError("x")] * 1030)
        config = HeartbeatConfig(
            master_url="http://127.0.0.1:8080",
            interval=1.0,
            timeout=0.5,
            max_backoff=60.0,
        )
        monitor = HeartbeatMonitor(config, sender.send, lambda: {"seq": 1})
        for _ in range(1030):
            self.assertFalse(await monitor.beat_once())
        self.assertEqual(monitor.next_delay(), 60.0)

    async def test_run_fence_survives_unexpected_errors(self) -> None:
        """run() 围栏兜住非 _RETRYABLE 异常：记一次失败并继续，不死。"""

        class _BombSender:
            """首次发送时抛出可重试面之外异常，之后恢复正常。"""

            def __init__(self) -> None:
                self.calls = 0

            async def send(self, payload: dict[str, object]) -> dict[str, object]:
                self.calls += 1
                if self.calls == 1:
                    raise AttributeError("载荷构造炸了")
                return {}

        bomb = _BombSender()
        monitor = HeartbeatMonitor(
            self._config(), bomb.send, lambda: {"seq": bomb.calls}
        )
        task = asyncio.create_task(monitor.run())
        try:
            ok = await wait_until(
                lambda: monitor.snapshot().failed >= 1, timeout=5.0, interval=0.01
            )
            self.assertTrue(ok)
            self.assertIn("AttributeError", monitor.snapshot().last_error or "")
            ok = await wait_until(
                lambda: monitor.snapshot().sent >= 1, timeout=5.0, interval=0.01
            )
            self.assertTrue(ok)
            snapshot = monitor.snapshot()
            self.assertEqual(snapshot.state, MasterState.ONLINE)
            self.assertEqual(snapshot.failed, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_run_loop_survives_failures(self) -> None:
        """run() 循环遇到失败不中断，成功后恢复正常间隔。"""
        sender = _ScriptedSender(
            [URLError("down"), URLError("down"), None, None, None]
        )
        monitor = self._monitor(sender)
        task = asyncio.create_task(monitor.run())
        try:
            ok = await wait_until(
                lambda: monitor.snapshot().sent >= 2, timeout=5.0, interval=0.01
            )
            self.assertTrue(ok)
            self.assertEqual(monitor.state, MasterState.ONLINE)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_server_config_applies_and_notifies(self) -> None:
        """主模块下发 config 即生效（含心跳间隔），并回调通知。"""
        sender = _ScriptedSender([{"config": {"interval": 0.5, "timeout": 2.0}}])
        changes: list[tuple[dict[str, tuple[float, float]], dict[str, str]]] = []
        monitor = HeartbeatMonitor(
            self._config(),
            sender.send,
            lambda: {"seq": 1},
            on_config=lambda applied, skipped: changes.append((applied, skipped)),
        )

        self.assertTrue(await monitor.beat_once())
        self.assertEqual(monitor.next_delay(), 0.5)
        self.assertEqual(len(changes), 1)
        self.assertEqual(
            changes[0][0], {"interval": (0.01, 0.5), "timeout": (0.5, 2.0)}
        )
        self.assertEqual(changes[0][1], {})

    async def test_server_config_skips_invalid_and_local_only(self) -> None:
        """非法值 / 仅限本地的键被跳过（保留原值），未知键静默忽略。"""
        sender = _ScriptedSender(
            [{"config": {"interval": -1, "master_url": "http://evil", "bogus": 1}}]
        )
        skipped: dict[str, str] = {}
        monitor = HeartbeatMonitor(
            self._config(),
            sender.send,
            lambda: {"seq": 1},
            on_config=lambda _applied, got: skipped.update(got),
        )

        self.assertTrue(await monitor.beat_once())
        self.assertEqual(monitor.next_delay(), 0.01)  # interval 未被改动
        self.assertIn("interval", skipped)
        self.assertIn("master_url", skipped)
        self.assertNotIn("bogus", skipped)  # 未知键不报，向前兼容
        self.assertEqual(monitor.snapshot().master_url, "http://127.0.0.1:8080")


class _RecordingMaster:
    """测试用主模块：起一个真实 HTTP 服务记录心跳载荷。

    ``reply`` 是 ``POST /api/heartbeat`` 的响应体（可放主模块下发的 config）。
    """

    def __init__(self, root: Path, port: int) -> None:
        self.received: list[dict[str, object]] = []
        self.reply: dict[str, object] = {"ok": True}
        self.context = Context()
        self.port = port
        self._config_path = root / f"master-{port}.ini"
        self._config_path.write_text(
            "[node]\n"
            "kind = master\n"
            "\n[web]\n"
            f"port = {port}\n",
            encoding="utf-8",
        )

    async def start(self) -> str:
        self.context.provide("node_kind", "master")
        await self.context.plugin(AppConfigPlugin, {"path": str(self._config_path)})
        await self.context.plugin(WebApiPlugin)
        received = self.received
        master = self

        @self.context.webapi.post("/api/heartbeat", kind="api")
        async def receive(request: Any) -> dict[str, object]:
            received.append(await request.json())
            return dict(master.reply)

        return self.context.webapi.address() or ""

    async def stop(self) -> None:
        await unload_all(self.context)


class HeartbeatPluginTests(unittest.IsolatedAsyncioTestCase):
    """插件集成：真实主模块在线 / 掉线 / 恢复 + 自省接口 + 卸载清理。"""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.master_port = _free_port()
        self.slave_port = _free_port()
        self.slave_base = f"http://127.0.0.1:{self.slave_port}"
        self.master = _RecordingMaster(self.root, self.master_port)
        self.slave: Context | None = None

    async def asyncTearDown(self) -> None:
        if self.slave is not None:
            await unload_all(self.slave)
        await self.master.stop()
        self.temp_dir.cleanup()

    def _write_slave_config(self, master_url: str) -> Path:
        path = self.root / "config.ini"
        path.write_text(
            "[node]\n"
            "kind = slave\n"
            "name = 从节点\n"
            "\n[heartbeat]\n"
            f"master_url = {master_url}\n"
            "interval = 0.05\n"
            "timeout = 1.0\n"
            "max_backoff = 0.2\n"
            "\n[web]\n"
            f"port = {self.slave_port}\n",
            encoding="utf-8",
        )
        return path

    async def _start_slave(
        self,
        config_path: Path,
        setup: Callable[[Context], object] | None = None,
    ) -> Context:
        context = Context()
        context.provide("node_kind", "slave")
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        context.provide(
            "node_name",
            context.appconfig.section("node").get("name") or socket.gethostname(),
        )
        await context.plugin(WebApiPlugin)
        if setup is not None:
            setup(context)  # 心跳任务启动前注册监听器，避免竞态
        await context.plugin(HeartbeatPlugin)
        self.slave = context
        return context

    async def _status(self) -> dict[str, object]:
        return await asyncio.to_thread(_status_dict, self.slave_base)

    async def _wait_state(self, target: str, timeout: float = 5.0) -> dict[str, object]:
        """等待自省接口状态变为 target（异步轮询，不卡事件循环），返回快照。"""
        deadline = time.monotonic() + timeout
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = await self._status()
            if status["state"] == target:
                return status
            await asyncio.sleep(0.05)
        self.fail(f"等待心跳状态 {target!r} 超时，最后快照：{status}")
        raise AssertionError("unreachable")

    async def test_master_online_receives_heartbeats(self) -> None:
        """主模块在线：心跳到达，状态接口报 ONLINE，载荷字段齐全。"""
        await self.master.start()
        await self._start_slave(self._write_slave_config(f"http://127.0.0.1:{self.master_port}"))

        ok = await wait_until(lambda: len(self.master.received) >= 2, timeout=5.0)
        self.assertTrue(ok)

        payload = self.master.received[0]
        self.assertEqual(payload["node_id"], "从节点")
        self.assertEqual(payload["node_kind"], "slave")
        self.assertEqual(payload["status"], "alive")
        self.assertIn("address", payload)
        self.assertIn("seq", payload)

        status = await self._status()
        self.assertEqual(status["state"], "online")
        self.assertEqual(status["master_url"], f"http://127.0.0.1:{self.master_port}")

    async def test_master_offline_then_online_recovers(self) -> None:
        """主模块先不在线再上线：offline → online，恢复后心跳继续。"""
        # 主模块不启动，先起从模块（对不存在的端口发心跳）
        await self._start_slave(self._write_slave_config(f"http://127.0.0.1:{self.master_port}"))

        offline_status = await self._wait_state("offline")
        self.assertTrue(offline_status["consecutive_failures"])
        self.assertTrue(offline_status["last_error"])

        # 主模块上线
        await self.master.start()
        await self._wait_state("online")
        self.assertTrue(
            await wait_until(lambda: len(self.master.received) >= 1, timeout=5.0)
        )

    async def test_master_drop_and_recover(self) -> None:
        """主模块中途掉线再恢复：状态从 online → offline → online。"""
        await self.master.start()
        await self._start_slave(self._write_slave_config(f"http://127.0.0.1:{self.master_port}"))
        await self._wait_state("online")

        # 主模块掉线
        await self.master.stop()
        await self._wait_state("offline")

        # 主模块恢复（新上下文同端口）
        self.master = _RecordingMaster(self.root, self.master_port)
        await self.master.start()
        await self._wait_state("online")

    async def test_status_route_reports_state(self) -> None:
        """GET /api/heartbeat/status 返回完整状态快照。"""
        await self.master.start()
        await self._start_slave(self._write_slave_config(f"http://127.0.0.1:{self.master_port}"))
        await self._wait_state("online")

        status = await self._status()
        for key in (
            "state",
            "master_url",
            "sent",
            "failed",
            "consecutive_failures",
            "last_success_at",
            "last_failure_at",
            "last_error",
            "next_retry_in",
            "extensions",
            "extension_errors",
            "capabilities",
            "caps_digest",
        ):
            self.assertIn(key, status)
        self.assertEqual(status["extensions"], {})
        self.assertEqual(status["extension_errors"], [])
        self.assertEqual(status["capabilities"], [])

    async def test_status_route_collects_extensions(self) -> None:
        """其他插件经 STATUS_EVENT 把自家数据插入 status 返回值。"""
        await self.master.start()
        context = await self._start_slave(
            self._write_slave_config(f"http://127.0.0.1:{self.master_port}")
        )

        def contribute(response: dict[str, object]) -> None:
            extensions = cast(dict[str, object], response["extensions"])
            extensions["demo"] = {"x": 1}

        context.on(STATUS_EVENT, contribute)
        await self._wait_state("online")

        status = await self._status()
        self.assertEqual(status["extensions"], {"demo": {"x": 1}})

    async def test_status_route_isolates_contributor_errors(self) -> None:
        """某个贡献者抛错不影响接口：错误进 extension_errors，其余照常返回。"""
        await self.master.start()
        context = await self._start_slave(
            self._write_slave_config(f"http://127.0.0.1:{self.master_port}")
        )

        def contribute(response: dict[str, object]) -> None:
            extensions = cast(dict[str, object], response["extensions"])
            extensions["good"] = {"ok": True}

        def broken(response: dict[str, object]) -> None:
            raise RuntimeError("贡献者坏了")

        context.on(STATUS_EVENT, contribute)
        context.on(STATUS_EVENT, broken)
        await self._wait_state("online")

        status = await self._status()
        self.assertEqual(status["extensions"], {"good": {"ok": True}})
        errors = cast(list[str], status["extension_errors"])
        self.assertEqual(len(errors), 1)
        self.assertIn("贡献者坏了", errors[0])

    async def test_unload_stops_heartbeat_task(self) -> None:
        """插件卸载后心跳任务停止：不再有新载荷到达主模块。"""
        await self.master.start()
        context = await self._start_slave(
            self._write_slave_config(f"http://127.0.0.1:{self.master_port}")
        )
        ok = await wait_until(lambda: len(self.master.received) >= 1, timeout=5.0)
        self.assertTrue(ok)

        await unload_all(context)
        self.slave = None
        # 负断言改「落定 + 观察」：先等卸载瞬间在途的请求收尾（计数不再增长），
        # 再观察约 10 个心跳周期（interval=0.05）确无新增——固定双 sleep 在
        # 慢 CI 上会被在途请求误报
        count_after_unload = len(self.master.received)
        for _ in range(40):
            await asyncio.sleep(0.05)
            current = len(self.master.received)
            if current == count_after_unload:
                break
            count_after_unload = current
        for _ in range(10):
            await asyncio.sleep(0.05)
            self.assertEqual(len(self.master.received), count_after_unload)

    async def test_master_config_syncs_interval(self) -> None:
        """端到端：主模块在心跳响应里下发 config，从模块同步生效。"""
        self.master.reply = {"ok": True, "config": {"interval": 0.3}}
        await self.master.start()
        await self._start_slave(
            self._write_slave_config(f"http://127.0.0.1:{self.master_port}")
        )
        await self._wait_state("online")

        status = await self._wait_interval(0.3)
        self.assertEqual(status["interval"], 0.3)

    async def test_capabilities_sync_by_digest(self) -> None:
        """能力同步：摘要一致时只发 caps_digest，主模块回 need_caps 才发全量。"""
        self.master.reply = {"ok": True}
        await self.master.start()

        def register(context: Context) -> None:
            def add_pytest(bucket: list[object]) -> None:
                bucket.append(
                    capability("executor", "pytest", "demo", summary="单测")
                )

            context.on(CAP_EVENT, add_pytest)

        await self._start_slave(
            self._write_slave_config(f"http://127.0.0.1:{self.master_port}"),
            setup=register,
        )
        ok = await wait_until(
            lambda: len(self.master.received) >= 3, timeout=5.0, interval=0.05
        )
        self.assertTrue(ok)

        digests = {
            str(payload.get("caps_digest")) for payload in self.master.received
        }
        self.assertEqual(len(digests), 1)  # 能力未变 → 摘要稳定
        full = [p for p in self.master.received if "capabilities" in p]
        lean = [p for p in self.master.received if "capabilities" not in p]
        self.assertGreaterEqual(len(full), 1)  # 首拍带全量
        self.assertGreaterEqual(len(lean), 1)  # 之后只带摘要
        self.assertEqual(
            full[0]["capabilities"],
            [capability("executor", "pytest", "demo", summary="单测")],
        )

        # 主模块要全量 → 下一拍重发 capabilities
        self.master.reply = {"ok": True, "need_caps": True}
        count = len(self.master.received)
        ok = await wait_until(
            lambda: any(
                "capabilities" in payload
                for payload in self.master.received[count:]
            ),
            timeout=5.0,
            interval=0.05,
        )
        self.assertTrue(ok)

    async def test_status_route_shows_capabilities(self) -> None:
        """状态接口返回能力信封列表与摘要，坏条目进 extension_errors。"""

        def contribute(bucket: list[object]) -> None:
            bucket.append(capability("executor", "pytest", "demo"))
            bucket.append({"broken": True})

        await self.master.start()
        await self._start_slave(
            self._write_slave_config(f"http://127.0.0.1:{self.master_port}"),
            setup=lambda context: context.on(CAP_EVENT, contribute),
        )
        await self._wait_state("online")

        status = await self._status()
        self.assertEqual(
            status["capabilities"],
            [{"kind": "executor", "id": "pytest", "provider": "demo"}],
        )
        self.assertIsInstance(status["caps_digest"], str)
        errors = cast(list[str], status["extension_errors"])
        self.assertTrue(any("不合信封" in error for error in errors))

    async def _wait_interval(
        self, expected: float, timeout: float = 5.0
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        status: dict[str, object] = {}
        while time.monotonic() < deadline:
            status = await self._status()
            if status["interval"] == expected:
                return status
            await asyncio.sleep(0.05)
        self.fail(f"等待心跳间隔变为 {expected} 超时，最后快照：{status}")
        raise AssertionError("unreachable")


class HeartbeatSenderTests(unittest.IsolatedAsyncioTestCase):
    """传输层异常归一化。"""

    async def test_connection_refused_becomes_send_error(self) -> None:
        config = HeartbeatConfig(
            master_url=f"http://127.0.0.1:{_free_port()}",
            interval=1.0,
            timeout=1.0,
            max_backoff=5.0,
        )
        sender = HeartbeatSender(config)
        with self.assertRaises(HeartbeatSendError) as caught:
            await sender.send({"seq": 1})
        self.assertIn("主模块", str(caught.exception))

    async def test_http_error_status_becomes_send_error(self) -> None:
        """主模块在线但接口返回 5xx：归一化为 HeartbeatSendError。"""
        with tempfile.TemporaryDirectory() as temporary:
            master = _RecordingMaster(Path(temporary), _free_port())
            # 利用不存在的路径触发 404
            address = await master.start()
            config = HeartbeatConfig(
                master_url=address or f"http://127.0.0.1:{master.port}",
                path="/api/missing",
                interval=1.0,
                timeout=1.0,
                max_backoff=5.0,
            )
            try:
                sender = HeartbeatSender(config)
                with self.assertRaises(HeartbeatSendError) as caught:
                    await sender.send({"seq": 1})
                self.assertIn("404", str(caught.exception))
            finally:
                await master.stop()

    async def test_oversized_response_becomes_send_error(self) -> None:
        """响应体超过上限（256 KiB）：按失败处理，防大响应撑爆内存。"""
        with tempfile.TemporaryDirectory() as temporary:
            master = _RecordingMaster(Path(temporary), _free_port())
            master.reply = {"pad": "x" * (300 * 1024)}
            address = await master.start()
            config = HeartbeatConfig(
                master_url=address or f"http://127.0.0.1:{master.port}",
                interval=1.0,
                timeout=5.0,
                max_backoff=5.0,
            )
            try:
                sender = HeartbeatSender(config)
                with self.assertRaises(HeartbeatSendError) as caught:
                    await sender.send({"seq": 1})
                self.assertIn("过大", str(caught.exception))
            finally:
                await master.stop()

    async def test_unserializable_payload_becomes_send_error(self) -> None:
        """载荷含不可序列化对象：归一化为 HeartbeatSendError，不裸抛。"""
        config = HeartbeatConfig(
            master_url=f"http://127.0.0.1:{_free_port()}",
            interval=1.0,
            timeout=1.0,
            max_backoff=5.0,
        )
        sender = HeartbeatSender(config)
        with self.assertRaises(HeartbeatSendError) as caught:
            await sender.send({"bad": object()})
        self.assertIn("序列化", str(caught.exception))

    def test_parse_response_body_rejects_deep_nesting(self) -> None:
        """深层嵌套 JSON 抛 RecursionError（非 ValueError）：按无配置处理。"""
        deep = b"[" * 50000 + b"]" * 50000
        self.assertEqual(parse_response_body(deep), {})

    def test_parse_response_body_rejects_non_object(self) -> None:
        """空 / 非 JSON / JSON 数组都按「无配置」返回空字典。"""
        self.assertEqual(parse_response_body(b""), {})
        self.assertEqual(parse_response_body(b"not-json"), {})
        self.assertEqual(parse_response_body(b"[1, 2]"), {})
        self.assertEqual(parse_response_body(b'{"config": {"a": 1}}'), {"config": {"a": 1}})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
