from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from cordis_port import Context

from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.comm import CommMeta, CommService, CommTopicHandler
from common.plugins.webapi import WebApiPlugin


def _collector(received: list[object]) -> CommTopicHandler:
    """把载荷收进列表的类型化处理器（lambda 参数无法注解）。"""

    def handler(payload: object, _meta: CommMeta) -> None:
        received.append(payload)

    return handler


class CommTests(unittest.IsolatedAsyncioTestCase):
    """comm：本地自环 / HTTP 批量上报 / 背压 / 退避 / 处理器隔离。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._temp.name)
        self.port = _free_port()
        self.context: Context | None = None

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self._temp.cleanup()

    async def boot(self, comm_ini: str) -> None:
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n"
            "\n[log]\n"
            "console = false\n"
            "\n[comm]\n"
            "batch_interval = 0.05\n"
            f"{comm_ini}",
            encoding="utf-8",
        )
        self.context = Context()
        self.context.provide("node_name", "test-node")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(CommService)

    async def metrics(self) -> dict[str, Any]:
        assert self.context is not None
        status, body = await _arequest(f"http://127.0.0.1:{self.port}", "/healthz")
        self.assertEqual(status, 200)
        return json.loads(body)["metrics"]["comm"]

    async def test_loopback_dispatch(self) -> None:
        """target 留空 = 本地自环：send 后 flush，处理器收到载荷与元数据。"""
        await self.boot("queue_max = 50\n")
        assert self.context is not None
        received: list[tuple[object, CommMeta]] = []

        def handler(payload: object, meta: CommMeta) -> None:
            received.append((payload, meta))

        self.context.comm.on("demo.topic", handler)
        self.context.comm.send("demo.topic", {"hello": "世界"})
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(len(received), 1)
        payload, meta = received[0]
        self.assertEqual(payload, {"hello": "世界"})
        self.assertEqual(meta["topic"], "demo.topic")
        self.assertEqual(meta["node"], "test-node")

    async def test_handler_errors_are_isolated(self) -> None:
        """单个处理器抛错不影响其他处理器（错误进日志不进调用方）。"""
        await self.boot("queue_max = 50\n")
        assert self.context is not None
        seen: list[object] = []

        def boom(_payload: object, _meta: CommMeta) -> None:
            raise RuntimeError("处理器炸了")

        self.context.comm.on("t", boom)
        self.context.comm.on("t", _collector(seen))
        self.context.comm.send("t", 1)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(seen, [1])

    async def test_backpressure_drops_oldest(self) -> None:
        """队列满丢最旧并计数（/healthz 可见）。"""
        await self.boot("queue_max = 2\n")
        assert self.context is not None
        received: list[object] = []
        self.context.comm.on("t", _collector(received))
        for index in range(5):
            self.context.comm.send("t", index)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(received, [3, 4])
        self.assertEqual((await self.metrics())["dropped"], 3)

    async def test_http_roundtrip_with_gzip_and_token(self) -> None:
        """target 指向自身：走批量 HTTP（gzip + 令牌）环回，处理器照常收到。"""
        await self.boot(f"target = http://127.0.0.1:{self.port}\nqueue_max = 50\n")
        assert self.context is not None
        received: list[object] = []
        self.context.comm.on("t", _collector(received))
        self.context.comm.send("t", {"via": "http"})
        await self.context.comm.flush(timeout=10.0)
        self.assertEqual(received, [{"via": "http"}])
        self.assertGreaterEqual(int((await self.metrics())["sent"]), 1)

    async def test_retry_when_target_unreachable(self) -> None:
        """目标不可达：退避重试持续发生（断网重连不丢的机制在跑）。"""
        await self.boot(
            "target = http://127.0.0.1:9\nretry_base = 0.01\nretry_cap = 0.02\nqueue_max = 50\n"
        )
        assert self.context is not None
        self.context.comm.send("t", {"ping": 1})
        deadline = time.monotonic() + 5.0
        retries = 0
        while time.monotonic() < deadline:
            retries = int((await self.metrics())["retries"])
            if retries >= 1:
                break
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(retries, 1)

    async def test_on_dispose_unregisters(self) -> None:
        """dispose 后处理器不再收到。"""
        await self.boot("queue_max = 50\n")
        assert self.context is not None
        received: list[object] = []
        dispose = self.context.comm.on("t", _collector(received))
        dispose()
        self.context.comm.send("t", 1)
        await self.context.comm.flush(timeout=5.0)
        self.assertEqual(received, [])

    async def test_send_rejects_invalid_input(self) -> None:
        """非法 topic / 不可序列化载荷拒收并计数。"""
        await self.boot("queue_max = 50\n")
        assert self.context is not None
        self.context.comm.send("", 1)
        self.context.comm.send("t", object())
        self.assertEqual((await self.metrics())["rejected"], 2)


if __name__ == "__main__":
    unittest.main()
