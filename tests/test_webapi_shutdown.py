"""webapi 关停收尾回归：ASGI lifespan 干净收场，不再打 CancelledError 关停噪音。

背景（实测日志）：浏览器 webui 长轮询 / keep-alive 在途时停主节点，uvicorn 优雅
收尾（``timeout_graceful_shutdown`` 缺省 None＝无限等）一直等连接清空，
``UvicornHost.stop`` 的强推窗口（start_timeout + 2s）耗尽后 ``task.cancel``
强杀 serve 任务；uvicorn 的 ``Server.shutdown`` 在 force_exit 分支**跳过**
``lifespan.shutdown()``，跑 ASGI lifespan 的主任务孤零零停在 ``receive()``，
直到 ``asyncio.run`` 收尾才被取消——Starlette 把该 CancelledError 的 traceback
当 ``lifespan.shutdown.failed`` 交 uvicorn 以 ERROR 打出（进程其实已正常退出，
纯属关停噪音）。

修复：① ``_GRACEFUL_SHUTDOWN_S`` 有界优雅等待，到点取消在途请求继续收尾；
② ``UvicornHost._reap_lifespan`` 兜底补发 lifespan 关停消息；③
``ShutdownNoiseFilter`` 压掉在途请求被取消的预期噪音。
"""

from __future__ import annotations

import asyncio
import logging
import time
import unittest
from unittest import mock

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from _helpers import AUTH_TOKEN, free_port
from common.plugins.webapi import uvicorn_web_service
from common.plugins.webapi.auth import AuthContext, SharedSecretVerifier
from common.plugins.webapi.config import WebApiConfig
from common.plugins.webapi.uvicorn_web_service import (
    ShutdownNoiseFilter,
    UvicornHost,
)


class ShutdownNoiseFilterTests(unittest.TestCase):
    """只丢关停预期噪音，真故障堆栈一个不挡。"""

    @staticmethod
    def _record(message: str, exc: BaseException | None = None) -> logging.LogRecord:
        return logging.LogRecord(
            name="uvicorn.error",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=(type(exc), exc, exc.__traceback__) if exc else None,
        )

    def test_drops_cancelled_and_grace_timeout_noise(self) -> None:
        noise_filter = ShutdownNoiseFilter()
        self.assertFalse(
            noise_filter.filter(
                self._record("Exception in ASGI application\n", asyncio.CancelledError())
            )
        )
        self.assertFalse(
            noise_filter.filter(
                self._record("Cancel 2 running task(s), timeout graceful shutdown exceeded")
            )
        )
        # Starlette 以 lifespan.shutdown.failed 上报的 traceback 文本（无 exc_info）
        self.assertFalse(
            noise_filter.filter(
                self._record(
                    "Traceback (most recent call last):\n"
                    '  File "...starlette/routing.py", line 655, in lifespan\n'
                    "    await receive()\n"
                    "asyncio.exceptions.CancelledError"
                )
            )
        )

    def test_keeps_real_errors(self) -> None:
        noise_filter = ShutdownNoiseFilter()
        self.assertTrue(
            noise_filter.filter(self._record("Exception in ASGI application\n", RuntimeError("boom")))
        )
        self.assertTrue(
            noise_filter.filter(
                self._record(
                    "Traceback (most recent call last):\n"
                    '  File "...app.py", line 1, in handler\n'
                    "RuntimeError: boom"
                )
            )
        )
        self.assertTrue(noise_filter.filter(self._record("Application startup failed. Exiting.")))


class UvicornHostShutdownTests(unittest.IsolatedAsyncioTestCase):
    """在途长请求（长轮询）场景下的 stop() 收尾行为。"""

    @staticmethod
    def _hang_server(start_timeout: float) -> tuple[UvicornHost, asyncio.Event, asyncio.Event]:
        """搭一个 ``/hang`` 永不返回的 host（模拟 webui 长轮询在途）。"""
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hang(_request: Request) -> Response:
            entered.set()
            await release.wait()
            return JSONResponse({"ok": True})

        config = WebApiConfig(
            host="127.0.0.1",
            port=free_port(),
            auth_token=AUTH_TOKEN,
            start_timeout=start_timeout,
        )
        host = UvicornHost(config, AuthContext(SharedSecretVerifier(config)))
        # Starlette 白名单：非 /api 路径免令牌，直连即可模拟浏览器在途请求
        host.app.add_route("/hang", hang)
        return host, entered, release

    @staticmethod
    async def _open_inflight_request(port: int) -> tuple[
        asyncio.StreamReader, asyncio.StreamWriter
    ]:
        """裸连接发一个请求后不读响应——服务器侧留一个在途 handler。"""
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /hang HTTP/1.1\r\nHost: test\r\n\r\n")
        await writer.drain()
        return reader, writer

    async def test_force_stop_reaps_lifespan_task(self) -> None:
        """强推路径（serve 被取消）也要补发 lifespan 关停，不留孤儿任务。"""
        host, entered, release = self._hang_server(start_timeout=2.0)
        await host.start()
        server = host.server
        assert server is not None
        _reader, writer = await self._open_inflight_request(host.config.port)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            begin = time.monotonic()
            await asyncio.wait_for(host.stop(), timeout=15)
            elapsed = time.monotonic() - begin
        finally:
            release.set()
            writer.close()
            await writer.wait_closed()

        # 兜底补发关停：lifespan 主任务已干净收场（不再是 asyncio.run 收尾的孤儿）
        shutdown_event = getattr(server.lifespan, "shutdown_event", None)
        assert shutdown_event is not None
        self.assertTrue(shutdown_event.is_set())
        # 强推窗口 start_timeout(2) + 2s，加补发兜底限时 2s
        self.assertLess(elapsed, 8.0)

    async def test_graceful_timeout_still_reaps_lifespan_quietly(self) -> None:
        """优雅等待超时（有界）后继续走 lifespan 关停，且关停噪音被滤掉。"""
        host, entered, release = self._hang_server(start_timeout=10.0)
        with mock.patch.object(uvicorn_web_service, "_GRACEFUL_SHUTDOWN_S", 1):
            await host.start()
            server = host.server
            assert server is not None
            _reader, writer = await self._open_inflight_request(host.config.port)
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                begin = time.monotonic()
                # 在途请求被取消 + 优雅超时提示都是预期关停语义，不应冒泡成 ERROR
                with self.assertNoLogs("uvicorn.error", level=logging.ERROR):
                    await asyncio.wait_for(host.stop(), timeout=15)
                elapsed = time.monotonic() - begin
            finally:
                release.set()
                writer.close()
                await writer.wait_closed()

        shutdown_event = getattr(server.lifespan, "shutdown_event", None)
        assert shutdown_event is not None
        self.assertTrue(shutdown_event.is_set())
        # 优雅等待 1s 即收尾，不再拖满 start_timeout(10) + 2s 的强推窗口
        self.assertLess(elapsed, 8.0)
