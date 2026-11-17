"""webapi 拦截链集成（Phase 1；§7.5 的 test_webapi_pipeline 项）。

真实 web 服务 + HookChain：顺序与注册序无关、鉴权拒绝短路、统计、
卸载摘除、自省接口、``kinds`` 过滤、``webapi/auth-denied`` 事件、
``/healthz`` 指标；另有鉴权验证器链（GAP-7）的单元检查。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from cordis_port import Context
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from _helpers import arequest, free_port
from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.webapi import WebApiPlugin
from common.plugins.webapi.auth import DENY, AuthContext, AuthIdentity, VerifyResult
from common.plugins.webapi.pipeline import MAX_ENHANCE_BODY_BYTES, RequestContext
from common.plugins.webapi.types import RouteRecord
from common.plugins.webapi.uvicorn_web_service import RequestObserverMiddleware
from master_plugins.api_stats import ApiStatsPlugin


class _StubWebUi:
    """最小 webui 桩：一条页面贡献（mounts 的 pages 汇集用）。"""

    def ui_contributions(self) -> list[object]:
        return [
            SimpleNamespace(
                id="demo:home", kind="page", owner="demo", path="/home", title="主页"
            )
        ]


class _StubVerifier:
    """记录调用顺序的验证器桩（返回预设结论）。"""

    def __init__(self, result: VerifyResult, log: list[str], label: str) -> None:
        self._result = result
        self._log = log
        self._label = label

    def verify(self, token: str, headers: Headers) -> VerifyResult:
        self._log.append(self._label)
        return self._result


class VerifierChainTests(unittest.TestCase):
    """有序验证器链（GAP-7）：order 有序、DENY 终止、兼容单槽写法。"""

    def test_chain_tries_in_order_until_identity(self) -> None:
        log: list[str] = []
        identity: AuthIdentity = {"subject": "u", "role": "admin"}
        auth = AuthContext(_StubVerifier(None, log, "first"))
        auth.register(_StubVerifier(identity, log, "second"), order=50)
        auth.register(_StubVerifier(None, log, "third"), order=100)
        self.assertEqual(auth.verify("t", Headers()), identity)
        self.assertEqual(log, ["first", "second"])  # 首个身份即停

    def test_deny_terminates_chain(self) -> None:
        log: list[str] = []
        auth = AuthContext(_StubVerifier(None, log, "first"))
        auth.register(_StubVerifier(DENY, log, "denier"), order=50)
        auth.register(
            _StubVerifier({"subject": "u", "role": "admin"}, log, "never"), order=100
        )
        self.assertIsNone(auth.verify("t", Headers()))
        self.assertEqual(log, ["first", "denier"])  # DENY 后不再尝试

    def test_compat_single_slot_get_and_set(self) -> None:
        first = _StubVerifier(None, [], "first")
        auth = AuthContext(first)
        self.assertIs(auth.verifier, first)
        replacement = _StubVerifier(None, [], "replacement")
        auth.verifier = replacement  # 旧单槽写法：整体替换
        self.assertIs(auth.verifier, replacement)
        auth.verify("t", Headers())
        # 注册的首个验证器随替换被摘：只有 replacement 被调用过


class PipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """真实 web 服务上的拦截链行为。"""

    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.port = free_port()
        config_path = self.root / "config.ini"
        config_path.write_text(
            "[node]\nkind = master\n"
            f"install_root = {(self.root / 'installed').as_posix()}\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n",
            encoding="utf-8",
        )
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.context: Context = Context()
        self.context.provide("node_kind", "master")
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(WebApiPlugin)

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self._temp.cleanup()

    def _ping(self) -> list[str]:
        calls: list[str] = []

        def ping(_request: object) -> dict[str, object]:
            calls.append("handler")
            return {"ok": True}

        self.context.webapi.get("/api/ping", ping, kind="api", name="ping")
        return calls

    async def test_order_independent_of_registration(self) -> None:
        trace: list[str] = []
        calls = self._ping()

        async def inner_first(rc: RequestContext, nxt: object) -> object:
            trace.append("inner")
            return await nxt()  # type: ignore[operator]

        async def observe_last(rc: RequestContext, nxt: object) -> object:
            trace.append("observe")
            assert rc.route.name == "ping"  # 拦截器可见路由元数据
            return await nxt()  # type: ignore[operator]

        self.context.webapi.use(inner_first, phase="inner", order=10)
        self.context.webapi.use(observe_last, phase="observe", order=200)
        status, _body = await arequest(self.base_url, "/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(trace, ["observe", "inner"])  # 与注册顺序相反
        self.assertEqual(calls, ["handler"])

    async def test_authorize_short_circuit_blocks_handler(self) -> None:
        calls = self._ping()

        async def deny(rc: RequestContext, nxt: object) -> object:
            return JSONResponse({"error": "无权限"}, status_code=403)  # 不调 next

        self.context.webapi.use(deny, phase="authorize", order=10)
        status, body = await arequest(self.base_url, "/api/ping")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "无权限")
        self.assertEqual(calls, [])  # handler 不会被执行

    async def test_kinds_filter_skips_non_api_routes(self) -> None:
        seen: list[str] = []

        def api_handler(_request: object) -> dict[str, object]:
            return {"kind": "api"}

        def page_handler(_request: object) -> str:
            return "page"

        self.context.webapi.get("/api/both", api_handler, kind="api", name="both-api")
        self.context.webapi.get("/page", page_handler, kind="route", name="both-page")

        async def only_api(rc: RequestContext, nxt: object) -> object:
            seen.append(rc.route.kind)
            return await nxt()  # type: ignore[operator]

        self.context.webapi.use(only_api, phase="observe", order=10, kinds=("api",))
        await arequest(self.base_url, "/api/both")
        await arequest(self.base_url, "/page")
        self.assertEqual(seen, ["api"])  # kind="route" 的路由不进链

    async def test_pipeline_introspection_and_health(self) -> None:
        async def probe(rc: RequestContext, nxt: object) -> object:
            return await nxt()  # type: ignore[operator]

        failed_once = False

        async def broken(rc: RequestContext, nxt: object) -> object:
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise ValueError("stats-probe")
            return await nxt()  # type: ignore[operator]

        self.context.webapi.use(probe, phase="observe", order=10, name="probe")
        self.context.webapi.use(broken, phase="guard", order=10, name="broken")
        self._ping()
        status, body = await arequest(self.base_url, "/api/web/pipeline")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            payload["phases"], ["observe", "guard", "authorize", "inner"]
        )
        hooks = {row["name"]: row for row in payload["hooks"]}
        self.assertEqual(hooks["probe"]["calls"], 1)
        self.assertEqual(hooks["broken"]["errors"], 1)  # SKIP 出错被计数
        self.assertEqual(hooks["broken"]["phase"], "guard")

        status, body = await arequest(self.base_url, "/healthz")
        metrics = json.loads(body)["metrics"]["pipeline"]
        self.assertEqual(metrics["hooks"], 2)
        self.assertEqual(metrics["errors"], 1)

    async def test_dispose_removes_hook(self) -> None:
        calls = self._ping()

        async def temporary(rc: RequestContext, nxt: object) -> object:
            calls.append("temporary")
            return await nxt()  # type: ignore[operator]

        dispose = self.context.webapi.use(temporary, phase="observe", order=10)
        await arequest(self.base_url, "/api/ping")
        self.assertEqual(calls, ["temporary", "handler"])
        dispose()
        calls.clear()
        await arequest(self.base_url, "/api/ping")
        self.assertEqual(calls, ["handler"])  # 注销后不再经过

    async def test_auth_denied_dispatches_event(self) -> None:
        seen: list[dict[str, object]] = []

        def _collect(payload: dict[str, object]) -> None:
            seen.append(payload)

        self.context.on("webapi/auth-denied", _collect)
        self._ping()
        status, _body = await arequest(self.base_url, "/api/ping", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["path"], "/api/ping")
        self.assertEqual(seen[0]["method"], "GET")
        self.assertEqual(seen[0]["reason"], "unauthorized")

    async def test_api_stats_counts_and_excludes(self) -> None:
        """api-stats 示例：按路由模板聚合；机器通道（/healthz）不统计。"""
        await self.context.plugin(ApiStatsPlugin)
        self._ping()
        await arequest(self.base_url, "/api/ping")
        await arequest(self.base_url, "/api/ping")
        status, body = await arequest(self.base_url, "/healthz")
        self.assertEqual(status, 200)
        stats = json.loads(body)["metrics"]["api_stats"]
        self.assertEqual(stats["requests"], 2)  # /healthz 自身被排除
        self.assertEqual(stats["top"][0]["path"], "/api/ping")
        self.assertEqual(stats["top"][0]["count"], 2)
        self.assertIn("p95", stats["top"][0])

    async def test_request_completed_event(self) -> None:
        """request-completed：每请求一条（含鉴权 401），载荷齐全。"""
        seen: list[dict[str, object]] = []

        def _collect(payload: dict[str, object]) -> None:
            seen.append(payload)

        self.context.on("webapi/request-completed", _collect)
        self._ping()
        status, _body = await arequest(self.base_url, "/api/ping")
        self.assertEqual(status, 200)
        record = await self._wait_for(lambda: seen[0] if seen else None)
        self.assertEqual(record["path"], "/api/ping")
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["route"], "ping")
        self.assertFalse(record["aborted"])
        self.assertGreaterEqual(cast(float, record["duration_ms"]), 0.0)
        self.assertGreater(cast(int, record["bytes"]), 0)
        # 鉴权短路（401）同样计入：观察者在 ASGI 栈最外层
        status, _body = await arequest(self.base_url, "/api/ping", token=None)
        self.assertEqual(status, 401)
        denied = await self._wait_for(lambda: seen[1] if len(seen) > 1 else None)
        self.assertEqual(denied["status"], 401)
        self.assertEqual(denied["route"], "")

    async def test_enhance_headers_query_body(self) -> None:
        """请求增强：headers / query / body 在 handler 视角生效。"""

        async def enrich(rc: RequestContext, nxt: object) -> object:
            rc.enhance(headers={"X-Trace": "42"}, query={"extra": "1"}, body=b"hello")
            return await nxt()  # type: ignore[operator]

        self.context.webapi.use(enrich, phase="guard", order=10)

        async def echo(request: Request) -> dict[str, object]:
            return {
                "header": request.headers.get("x-trace"),
                "query": request.query_params.get("extra"),
                "body": (await request.body()).decode(),
            }

        self.context.webapi.get("/api/echo", echo, kind="api", name="echo")
        status, body = await arequest(self.base_url, "/api/echo")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["header"], "42")
        self.assertEqual(payload["query"], "1")
        self.assertEqual(payload["body"], "hello")

    def test_enhance_rejects_oversized_body(self) -> None:
        """请求增强 body 超限直接拒绝（防止把大 body 缓存进内存）。"""
        request = Request(
            cast(
                "Any",
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/x",
                    "headers": [],
                    "query_string": b"",
                },
            )
        )
        rc = RequestContext(
            request=request,
            route=RouteRecord(path="/api/x", kind="api"),
            identity=None,
        )
        with self.assertRaises(ValueError):
            rc.enhance(body=b"x" * (MAX_ENHANCE_BODY_BYTES + 1))

    async def test_mounts_catalog(self) -> None:
        """挂载资源清单：端点 + 页面（webui 动态汇集），供权限精细匹配。"""
        self._ping()
        self.context.provide("webui", _StubWebUi())
        status, body = await arequest(self.base_url, "/api/web/mounts")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        endpoints = {item["name"]: item for item in payload["endpoints"]}
        self.assertEqual(endpoints["ping"]["path"], "/api/ping")
        self.assertEqual(endpoints["ping"]["methods"], ["GET", "HEAD"])
        self.assertIn("web.mounts", endpoints)
        pages = {item["id"]: item for item in payload["pages"]}
        self.assertEqual(pages["demo:home"]["path"], "/home")
        self.assertEqual(pages["demo:home"]["owner"], "demo")

    async def _wait_for(
        self, fetch: Callable[[], dict[str, object] | None], timeout: float = 2.0
    ) -> dict[str, object]:
        """轮询等待事件到达（响应发完与客户端收完之间存在微小时差）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            item = fetch()
            if item is not None:
                return item
            await asyncio.sleep(0.02)
        self.fail("等待 request-completed 事件超时")
        raise AssertionError("unreachable")


class ObserverMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """观察者中间件的取消 / 断连语义（HTTP 层模拟不到的路径）。"""

    async def test_dispatch_survives_double_cancellation(self) -> None:
        """取消传播中事件照发（shield 兜底）：aborted 置位、载荷完整。"""
        seen: list[dict[str, object]] = []
        started = asyncio.Event()

        async def emit(payload: dict[str, object]) -> None:
            await asyncio.sleep(0.05)  # 模拟 parallel_event 的异步派发
            seen.append(payload)

        async def hanging_app(_scope: Scope, _receive: Receive, _send: Send) -> None:
            started.set()
            await asyncio.Event().wait()  # 挂起直到被取消

        async def never_receive() -> Message:
            await asyncio.Event().wait()
            return cast("Message", {"type": "http.disconnect"})

        async def record_send(_message: Message) -> None:
            return None

        middleware = RequestObserverMiddleware(hanging_app, emit=emit)
        scope = cast("Scope", {"type": "http", "path": "/api/x", "method": "GET"})
        task = asyncio.create_task(middleware(scope, never_receive, record_send))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0.01)  # 让取消进入 unwind，第二次取消打在派发上
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(100):  # shield 的后台派发随后完成
            if seen:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["aborted"])
        self.assertEqual(seen[0]["path"], "/api/x")
        self.assertEqual(seen[0]["status"], 0)

    async def test_disconnect_marks_aborted(self) -> None:
        """观察到 http.disconnect 时 aborted 置位。"""
        seen: list[dict[str, object]] = []

        def emit(payload: dict[str, object]) -> None:
            seen.append(payload)

        async def app(_scope: Scope, receive: Receive, _send: Send) -> None:
            await receive()  # 读到 http.disconnect

        async def disconnect_receive() -> Message:
            return cast("Message", {"type": "http.disconnect"})

        async def record_send(_message: Message) -> None:
            return None

        middleware = RequestObserverMiddleware(app, emit=emit)
        scope = cast("Scope", {"type": "http", "path": "/api/y", "method": "GET"})
        await middleware(scope, disconnect_receive, record_send)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["aborted"])


if __name__ == "__main__":
    unittest.main()
