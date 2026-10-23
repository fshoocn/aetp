from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, cast

from cordis_port import Context

from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import (
    FIRE_AND_FORGET,
    CommMeta,
    CommService,
    CommTopicHandler,
)
from common.plugins.db import DbPlugin
from common.plugins.webapi import WebApiPlugin

#: 不可达目标（连接被拒）：触发重试 / 上限语义
DEAD_TARGET = "http://127.0.0.1:9"


def _collector(received: list[object]) -> CommTopicHandler:
    """把载荷收进列表的类型化处理器（lambda 参数无法注解）。"""

    def handler(payload: object, _meta: CommMeta) -> None:
        received.append(payload)

    return handler


class CommTests(unittest.IsolatedAsyncioTestCase):
    """comm：自环 / HTTP 上行 / QoS 策略 / 广播 / 定向 / outbox / 事件桥。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name)
        self.port = _free_port()
        self.context: Context | None = None

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self._temp.cleanup()

    def _write_config(self, comm_ini: str, *, kind: str = "master") -> Path:
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\n"
            f"kind = {kind}\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n"
            "\n[log]\n"
            "console = false\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(self.root / 'aetp.db').as_posix()}\n"
            "\n[comm]\n"
            f"{comm_ini}",
            encoding="utf-8",
        )
        return config_path

    async def boot(
        self, comm_ini: str, *, node_name: str = "test-node", kind: str = "master"
    ) -> None:
        config_path = self._write_config(comm_ini, kind=kind)
        self.context = Context()
        self.context.provide("node_name", node_name)
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(CommService)

    async def metrics(self) -> dict[str, Any]:
        assert self.context is not None
        status, body = await _arequest(f"http://127.0.0.1:{self.port}", "/healthz")
        self.assertEqual(status, 200)
        return cast(dict[str, Any], json.loads(body)["metrics"]["comm"])

    async def poll(self, node: str, *, hang: float = 0.0) -> list[dict[str, Any]]:
        """模拟从节点长轮询取下行件。"""
        status, body = await _arequest(
            f"http://127.0.0.1:{self.port}",
            "/api/comm/poll",
            method="POST",
            body=json.dumps({"node": node, "hang": hang, "max": 10}).encode("utf-8"),
            content_type="application/json",
        )
        self.assertEqual(status, 200)
        return cast(list[dict[str, Any]], json.loads(body)["items"])

    async def ack(self, node: str, ids: list[str]) -> None:
        """模拟从节点确认下行件。"""
        status, _body = await _arequest(
            f"http://127.0.0.1:{self.port}",
            "/api/comm/ack",
            method="POST",
            body=json.dumps({"node": node, "ids": ids}).encode("utf-8"),
            content_type="application/json",
        )
        self.assertEqual(status, 200)

    async def wait_state(
        self, message_id: str, wanted: str, *, timeout: float = 5.0
    ) -> dict[str, Any]:
        """轮询等待消息进入目标状态。"""
        assert self.context is not None
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status = self.context.comm.status(message_id)
            if status is not None:
                last = cast(dict[str, Any], status)
                if last["state"] == wanted:
                    return last
            await asyncio.sleep(0.02)
        self.fail(f"消息 {message_id} 未进入 {wanted} 状态：{last}")

    # ------------------------------------------------------------- 基本收发
    async def test_loopback_dispatch(self) -> None:
        """target 留空 = 本地自环：report 后处理器收到载荷与元数据。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        received: list[tuple[object, CommMeta]] = []

        def handler(payload: object, meta: CommMeta) -> None:
            received.append((payload, meta))

        self.context.comm.on("demo.topic", handler)
        mid = self.context.comm.report("demo.topic", {"hello": "世界"})
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(len(received), 1)
        payload, meta = received[0]
        self.assertEqual(payload, {"hello": "世界"})
        self.assertEqual(meta["topic"], "demo.topic")
        self.assertEqual(meta["node"], "test-node")
        self.assertEqual(meta["id"], mid)
        self.assertEqual(self.context.comm.status(mid)["state"], "done")  # type: ignore[index]

    async def test_handler_errors_are_isolated(self) -> None:
        """单个处理器抛错不影响其他处理器（错误进日志不进调用方）。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        seen: list[object] = []

        def boom(_payload: object, _meta: CommMeta) -> None:
            raise RuntimeError("处理器炸了")

        self.context.comm.on("t", boom)
        self.context.comm.on("t", _collector(seen))
        self.context.comm.report("t", 1)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(seen, [1])

    async def test_backpressure_drops_oldest(self) -> None:
        """在途消息超限丢最旧并计数（/healthz 可见）。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 2\n")
        assert self.context is not None
        received: list[object] = []
        self.context.comm.on("t", _collector(received))
        for index in range(5):
            self.context.comm.report("t", index)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(received, [3, 4])
        self.assertEqual((await self.metrics())["dropped"], 3)

    async def test_on_dispose_unregisters(self) -> None:
        """dispose 后处理器不再收到。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        received: list[object] = []
        dispose = self.context.comm.on("t", _collector(received))
        dispose()
        self.context.comm.report("t", 1)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(received, [])

    async def test_nodes_lists_known_nodes(self) -> None:
        """nodes() 列出全部已知节点：自身 + 上报/轮询过的节点。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        self.assertEqual(self.context.comm.nodes(), ["test-node"])
        await self.poll("slave-x")
        self.assertEqual(self.context.comm.nodes(), ["slave-x", "test-node"])

    async def test_send_rejects_invalid_input(self) -> None:
        """非法 topic / 不可序列化载荷 / 缺目标节点拒收并计数。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        self.assertEqual(self.context.comm.report("", 1), "")
        self.assertEqual(self.context.comm.report("t", object()), "")
        self.assertEqual(self.context.comm.send_to("", "t", 1), "")
        self.assertEqual((await self.metrics())["rejected"], 3)

    # ------------------------------------------------------------- HTTP 上行
    async def test_http_roundtrip_with_gzip_and_token(self) -> None:
        """target 指向自身：gzip + 令牌环回；中文节点名也能发（回归：头限 latin-1）。"""
        await self.boot(
            f"batch_interval = 0.05\ntarget = http://127.0.0.1:{self.port}\nqueue_max = 50\n",
            node_name="从节点甲",
        )
        assert self.context is not None
        received: list[object] = []
        self.context.comm.on("t", _collector(received))
        mid = self.context.comm.report("t", {"via": "http"})
        await self.context.comm.flush(timeout=10.0)
        await asyncio.sleep(0.2)  # ingest 侧派发是异步任务，给一拍收尾
        self.assertEqual(received, [{"via": "http"}])
        self.assertEqual(self.context.comm.status(mid)["state"], "done")  # type: ignore[index]
        self.assertGreaterEqual(int((await self.metrics())["sent"]), 1)

    async def test_retry_when_target_unreachable(self) -> None:
        """目标不可达：退避重试持续发生（断网重连不丢的机制在跑）。"""
        await self.boot(
            f"batch_interval = 0.05\ntarget = {DEAD_TARGET}\n"
            "retry_base = 0.01\nretry_cap = 0.02\nqueue_max = 50\n"
        )
        assert self.context is not None
        self.context.comm.report("t", {"ping": 1})
        deadline = time.monotonic() + 5.0
        retries = 0
        while time.monotonic() < deadline:
            retries = int((await self.metrics())["retries"])
            if retries >= 1:
                break
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(retries, 1)

    # ------------------------------------------------------------- QoS 策略
    async def test_policy_at_least_once(self) -> None:
        """min_success=1（默认）：成功前持续重试，成功即 DONE。"""
        await self.boot(
            f"batch_interval = 0.05\ntarget = http://127.0.0.1:{self.port}\nqueue_max = 50\n"
        )
        assert self.context is not None
        mid = self.context.comm.report("t", {"x": 1})
        status = await self.wait_state(mid, "done", timeout=10.0)
        self.assertGreaterEqual(status["acks"], 1)

    async def test_policy_max_attempts(self) -> None:
        """max_attempts=x：恰好发 x 次后 FAILED（至多发送 x 次）。"""
        await self.boot(
            f"batch_interval = 0.05\ntarget = {DEAD_TARGET}\n"
            "retry_base = 0.01\nretry_cap = 0.02\nqueue_max = 50\n"
        )
        assert self.context is not None
        mid = self.context.comm.report(
            "t", {"x": 1}, policy={"min_success": 1, "max_attempts": 3}
        )
        status = await self.wait_state(mid, "failed", timeout=10.0)
        self.assertEqual(status["attempts"], 3)

    async def test_policy_min_retries_floor_conflict_cap_wins(self) -> None:
        """min_retries 与 max_attempts 冲突：硬上限优先（决策 D2）。"""
        await self.boot(
            f"batch_interval = 0.05\ntarget = {DEAD_TARGET}\n"
            "retry_base = 0.01\nretry_cap = 0.02\nqueue_max = 50\n"
        )
        assert self.context is not None
        mid = self.context.comm.report(
            "t",
            {"x": 1},
            policy={"min_success": 1, "min_retries": 5, "max_attempts": 2},
        )
        status = await self.wait_state(mid, "failed", timeout=10.0)
        self.assertEqual(status["attempts"], 2)

    async def test_policy_4xx_no_retry(self) -> None:
        """协议错误（4xx）直接 FAILED、不吃保底重试（决策 D3）。"""
        await self.boot(
            f"batch_interval = 0.05\ntarget = http://127.0.0.1:{self.port}/nope\nqueue_max = 50\n"
        )
        assert self.context is not None
        mid = self.context.comm.report(
            "t", {"x": 1}, policy={"min_success": 1, "min_retries": 3, "max_attempts": 0}
        )
        status = await self.wait_state(mid, "failed", timeout=10.0)
        self.assertEqual(status["attempts"], 1)
        self.assertEqual((await self.metrics())["retries"], 0)

    async def test_immediate_channel(self) -> None:
        """immediate=True 不攒批（心跳通道）：批量间隔再大也不被拖慢。"""
        await self.boot("batch_interval = 5.0\nqueue_max = 50\n")
        assert self.context is not None
        received: list[object] = []
        self.context.comm.on("hb", _collector(received))
        self.context.comm.report("hb", 1, policy=FIRE_AND_FORGET, immediate=True)
        await self.context.comm.flush(timeout=2.0)
        self.assertEqual(received, [1])

    # --------------------------------------------------------- 广播 / 定向
    async def test_broadcast_any_success_cancels_rest(self) -> None:
        """广播任一目标成功即 DONE（A4），其余目标的待发记录撤销。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        await self.poll("slave-a")  # poll 即注册节点
        await self.poll("slave-b")
        mid = self.context.comm.broadcast("bc", {"x": 1})
        items = await self.poll("slave-a", hang=1.0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["topic"], "bc")
        await self.ack("slave-a", [str(items[0]["id"])])
        status = await self.wait_state(mid, "done", timeout=5.0)
        self.assertGreaterEqual(status["acks"], 1)
        # 其余目标被撤销：取不到件
        self.assertEqual(await self.poll("slave-b", hang=0.2), [])

    async def test_broadcast_without_remote_dispatches_locally(self) -> None:
        """无远端节点：本地派发即"发出去"，计入成功数（单节点部署可用）。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        received: list[object] = []
        self.context.comm.on("bc", _collector(received))
        mid = self.context.comm.broadcast("bc", {"x": 1})
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(received, [{"x": 1}])
        self.assertEqual(self.context.comm.status(mid)["state"], "done")  # type: ignore[index]

    async def test_directed_delivery(self) -> None:
        """send_to 只到目标节点；定向给自己 = 本地派发。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        await self.poll("slave-a")
        mid = self.context.comm.send_to("slave-a", "job", {"n": 1})
        items = await self.poll("slave-a", hang=1.0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["topic"], "job")
        self.assertEqual(items[0]["to"], "slave-a")
        self.assertEqual(
            self.context.comm.status(mid)["state"], "in_flight"  # type: ignore[index]
        )
        await self.ack("slave-a", [str(items[0]["id"])])
        self.assertEqual(
            self.context.comm.status(mid)["state"], "done"  # type: ignore[index]
        )

        received: list[object] = []
        self.context.comm.on("self.t", _collector(received))
        self.context.comm.send_to("test-node", "self.t", 42)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(received, [42])

    async def test_downlink_in_flight_timeout_retries(self) -> None:
        """下行取件后不 ack：ack 超时按策略重投（at-least-once 兜底）。"""
        await self.boot(
            "batch_interval = 0.05\nqueue_max = 50\nack_timeout = 0.2\nretry_base = 0.01\nretry_cap = 0.02\n"
        )
        assert self.context is not None
        await self.poll("slave-a")
        self.context.comm.send_to("slave-a", "job", {"n": 1})
        items = await self.poll("slave-a", hang=1.0)
        self.assertEqual(len(items), 1)
        deadline = time.monotonic() + 5.0
        redelivered: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            redelivered = await self.poll("slave-a", hang=0.2)
            if redelivered:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(redelivered), 1)  # 超时后重投

    # ------------------------------------------------------------- 事件桥
    async def test_cordis_event_dispatch(self) -> None:
        """comm.on 与 ctx.on("comm/{topic}") 等价：都能收到派发。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        got_bus: list[object] = []
        got_on: list[object] = []
        self.context.on("comm/t", _collector(got_bus))
        self.context.comm.on("t", _collector(got_on))
        self.context.comm.report("t", 7)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(got_bus, [7])
        self.assertEqual(got_on, [7])

    async def test_relay_roundtrip(self) -> None:
        """relay：本地事件 → 网络 → 对端本地事件（线上 topic relay/{event}）。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        got: list[object] = []
        self.context.comm.relay("evt.out", direction="out", topic="relay/x")
        self.context.comm.relay("evt.in", direction="in", topic="relay/x")

        def listener(payload: object) -> None:
            got.append(payload)

        self.context.on("evt.in", listener)
        self.context.emit("evt.out", {"k": 1})
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(got, [{"k": 1}])
        self.assertEqual((await self.metrics())["relays"]["out"], 1)  # type: ignore[index]
        self.assertEqual((await self.metrics())["relays"]["in"], 1)  # type: ignore[index]

    # ------------------------------------------------------------- 持久化
    async def test_outbox_persist_and_recover(self) -> None:
        """min_success>=1 落盘、成功即删；重启恢复续传（决策 A1）。"""
        comm_ini = (
            f"batch_interval = 0.05\ntarget = {DEAD_TARGET}\n"
            "retry_base = 0.05\nretry_cap = 0.1\nqueue_max = 50\n"
        )
        await self.boot(comm_ini)
        assert self.context is not None
        mid = self.context.comm.report(
            "t", {"k": 1}, policy={"min_success": 1, "max_attempts": 50}
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if int((await self.metrics())["outbox_disk"]) >= 1:
                break
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(int((await self.metrics())["outbox_disk"]), 1)

        await unload_all(self.context)  # 模拟进程重启
        await self.boot(comm_ini)
        assert self.context is not None
        self.assertGreaterEqual(int((await self.metrics())["outbox_disk"]), 1)
        status = self.context.comm.status(mid)
        self.assertIsNotNone(status)  # 恢复后仍可查、仍在续传
        self.assertEqual(status["state"], "retry_wait")  # type: ignore[index]

    async def test_outbox_fire_and_forget_not_persisted(self) -> None:
        """min_success=0 不落盘（心跳类流量）。"""
        await self.boot("batch_interval = 0.05\nqueue_max = 50\n")
        assert self.context is not None
        received: list[object] = []
        self.context.comm.on("hb", _collector(received))
        self.context.comm.report("hb", 1, policy=FIRE_AND_FORGET)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(received, [1])
        self.assertEqual((await self.metrics())["outbox_disk"], 0)

    # ------------------------------------------------------------- 传输契约
    async def test_transport_contract_with_fake(self) -> None:
        """Core 与驱动解耦：换 Transport 实现（Fake）语义不变。"""
        await self.boot(
            f"batch_interval = 0.05\ntarget = {DEAD_TARGET}\nqueue_max = 50\n"
        )
        assert self.context is not None
        calls: list[list[Any]] = []

        class FakeTransport:
            async def deliver(self, items: list[Any]) -> dict[str, Any]:
                calls.append(items)
                return {"ok": True, "error_kind": ""}

            async def pull(self, hang: float) -> list[Any]:
                return []

            async def ack(self, ids: list[str]) -> None:
                return None

            async def close(self) -> None:
                return None

        self.context.comm._transport = cast(Any, FakeTransport())
        mid = self.context.comm.report("t", {"x": 1})
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(self.context.comm.status(mid)["state"], "done")  # type: ignore[index]
        self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
