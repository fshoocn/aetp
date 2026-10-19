from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cordis_port import Context
from starlette.requests import Request as StarletteRequest

from _helpers import AUTH_TOKEN
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.webapi import WebApiPlugin
from common.plugins.webapi.plugin_manager_plugin import PluginManagerPlugin
from common.plugins.webapi.router import RouteRegistry
from common.plugins.webapi.types import RouteRecord
from master.webui.plugin import owner_slug

_SOURCE_ROOT: Path = Path(__file__).resolve().parents[1]


def _request(
    base_url: str,
    path: str,
    method: str = "GET",
    token: str | None = AUTH_TOKEN,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, str, bytes]:
    headers = dict(extra_headers or {})
    if token:
        headers["X-AETP-Token"] = token
    request = Request(f"{base_url}{path}", method=method, headers=headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        status = response.status
        body = response.read()
        if not isinstance(status, int) or not isinstance(body, bytes):
            raise TypeError("HTTP 响应的状态码或正文类型无效")
        return status, response.headers.get("Content-Type") or "", body


class RouteRegistryTests(unittest.TestCase):
    def test_owner_cleanup_preserves_other_methods_on_same_path(self) -> None:
        class EffectOwner:
            def __init__(self) -> None:
                self.cleanup: Callable[[], bool] | None = None
                self.labels: list[str] = []

            def effect(
                self,
                execute: Callable[[], Callable[[], bool] | None],
                label: str = "anonymous",
            ) -> Callable[[], bool]:
                self.labels.append(label)
                cleanup = execute()
                if not callable(cleanup):
                    raise TypeError("同步 effect 必须返回销毁函数")
                self.cleanup = cleanup
                return lambda: True

            def dispose(self) -> None:
                if self.cleanup is not None:
                    self.cleanup()

        registry = RouteRegistry()
        get_owner = EffectOwner()
        post_owner = EffectOwner()

        def get_handler(_: StarletteRequest) -> dict[str, str]:
            return {"method": "GET"}

        def post_handler(_: StarletteRequest) -> dict[str, str]:
            return {"method": "POST"}

        get_route = RouteRecord(
            path="/shared",
            kind="api",
            methods=frozenset({"GET"}),
            handler=get_handler,
        )
        post_route = RouteRecord(
            path="/shared",
            kind="api",
            methods=frozenset({"POST"}),
            handler=post_handler,
        )

        registry.add(get_route, get_owner)
        registry.add(post_route, post_owner)
        self.assertEqual(get_owner.labels, ["ctx.webapi.route('/shared')"])
        get_owner.dispose()

        self.assertEqual(registry.get_all("/shared"), [post_route])


class OwnerSlugTests(unittest.TestCase):
    """UI 路径标识：撞键确定性消歧（防 api.remove 前缀删除误伤他人挂载）。"""

    def test_collision_gets_deterministic_suffix(self) -> None:
        self.assertEqual(owner_slug("a.b"), "a-b")
        self.assertEqual(owner_slug("a b"), "a-b")
        slug = owner_slug("a b", taken=["a-b"])
        self.assertTrue(slug.startswith("a-b-"))
        self.assertNotEqual(slug, "a-b")
        # 同输入同输出：稳定绑定，跨注册顺序不变
        self.assertEqual(slug, owner_slug("a b", taken=["a-b"]))
        with self.assertRaisesRegex(ValueError, "无法消歧"):
            owner_slug("a b", taken=["a-b", slug])


class SampleUiPlugin:
    name: ClassVar[str] = "test-ui"
    inject: ClassVar[list[str]] = ["webapi", "webui"]

    def __init__(self, ctx: Context, config: dict[str, str]) -> None:
        ctx.webui.register_ui(
            "dashboard",
            kind="page",
            format="vue",
            resource_root=config["resource_root"],
            entry="Dashboard.vue",
            path="/plugins/test-ui",
            title="Test UI",
            menu_group="Tests",
        )


class WebUiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context: Context = Context()
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.port: int = _free_port()
        self.context.provide("node_kind", "master")
        # 自备 SPA 产物替身：真实产物（static/ui/dist）是 gitignored 的构建输出，
        # 测试不能依赖前端已构建（新克隆 / CI 上没有它照样要能跑）
        ui_dist = Path(self.temp_dir.name) / "ui-dist"
        ui_dist.mkdir()
        (ui_dist / "index.html").write_text(
            '<!doctype html><script type="module" src="/ui-static/assets/app.js"></script>',
            encoding="utf-8",
        )
        config_path = Path(self.temp_dir.name) / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            f"install_root = {Path(self.temp_dir.name) / 'installed'}\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n"
            "\n[webui]\n"
            f"ui_directory = {ui_dist}\n",
            encoding="utf-8",
        )
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(PluginManagerPlugin)
        plugins = self.context.plugins
        await plugins.install_source(_SOURCE_ROOT / "master" / "webui")
        await plugins.enable("webui")

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_manifest_assets_spa_fallback_and_fiber_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            component_path = Path(temporary_directory, "Dashboard.vue")
            component_source = "<template><h1>plugin page</h1></template>"
            component_path.write_text(component_source, encoding="utf-8")
            fiber = await self.context.plugin(
                SampleUiPlugin,
                {"resource_root": temporary_directory},
            )
            base_url = f"http://127.0.0.1:{self.port}"

            status, content_type, body = await asyncio.to_thread(_request, base_url, "/api/web/ui")
            self.assertEqual(status, 200)
            self.assertIn("application/json", content_type)
            manifest = json.loads(body)
            self.assertEqual(len(manifest), 1)
            self.assertEqual(manifest[0]["owner"], "test-ui")
            self.assertEqual(manifest[0]["path"], "/plugins/test-ui")

            status, _, body = await asyncio.to_thread(_request, base_url, manifest[0]["entry"])
            self.assertEqual(status, 200)
            self.assertEqual(body.decode("utf-8"), component_source)

            status, content_type, body = await asyncio.to_thread(_request, base_url, "/plugins/test-ui")
            self.assertEqual(status, 200)
            self.assertIn("text/html", content_type)
            self.assertIn(b"/ui-static/assets/", body)

            self.assertEqual((await asyncio.to_thread(_request, base_url, "/api/missing"))[0], 404)
            self.assertEqual((await asyncio.to_thread(_request, base_url, "/ui-static/missing.js"))[0], 404)

            await fiber.dispose()
            status, _, body = await asyncio.to_thread(_request, base_url, "/api/web/ui")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), [])
            self.assertEqual((await asyncio.to_thread(_request, base_url, manifest[0]["entry"]))[0], 404)

    async def test_unified_error_envelope(self) -> None:
        """错误信封统一 ``{"error"}``：404 与路径参数转换失败的 400 都带真实原因。"""
        def typed_handler(_request: StarletteRequest, item_id: int) -> dict[str, int]:
            return {"item_id": item_id}

        self.context.webapi.get(
            "/api/items/{item_id}", typed_handler, kind="api", name="items.get"
        )
        base_url = f"http://127.0.0.1:{self.port}"

        status, _, body = await asyncio.to_thread(_request, base_url, "/api/items/abc")
        self.assertEqual(status, 400)
        self.assertIn("item_id", json.loads(body)["error"])

        status, _, body = await asyncio.to_thread(_request, base_url, "/api/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "Not Found")

    async def test_healthz_reports_uptime_and_metrics(self) -> None:
        """/healthz 存活探针免令牌，返回 uptime 与指标快照。"""
        response = await asyncio.to_thread(
            urlopen, Request(f"http://127.0.0.1:{self.port}/healthz"), timeout=5
        )
        with response:
            payload = json.loads(response.read())
        self.assertEqual(payload["status"], "ok")
        self.assertGreaterEqual(payload["uptime_s"], 0)
        self.assertIsInstance(payload["metrics"], dict)

    async def test_security_headers(self) -> None:
        """响应统一携带 nosniff 安全头（静态与 API 同款）。"""
        request = Request(
            f"http://127.0.0.1:{self.port}/api/web/ui",
            headers={"X-AETP-Token": AUTH_TOKEN},
        )
        response = await asyncio.to_thread(urlopen, request, timeout=5)
        with response:
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    async def test_static_cache_policy(self) -> None:
        """HTML / 未 hash 静态 no-cache（导航必回源）；hash 产物 immutable 长缓存。

        回归：静态响应原先不带 Cache-Control，浏览器启发式缓存旧产物，
        首次导航拿到旧 UI（弹窗缺新字段），刷新才更新。
        """
        assets = Path(self.temp_dir.name) / "ui-dist" / "assets"
        assets.mkdir()
        (assets / "app-12345678.js").write_text("console.log(1)", encoding="utf-8")
        base_url = f"http://127.0.0.1:{self.port}"

        response = await asyncio.to_thread(
            urlopen, Request(f"{base_url}/ui-static/index.html"), timeout=5
        )
        with response:
            self.assertEqual(response.headers["Cache-Control"], "no-cache")

        # SPA 深层回退返回的 HTML 同样 no-cache
        response = await asyncio.to_thread(
            urlopen, Request(f"{base_url}/plugins/any/deep/link"), timeout=5
        )
        with response:
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertEqual(response.headers["Cache-Control"], "no-cache")

        response = await asyncio.to_thread(
            urlopen, Request(f"{base_url}/ui-static/assets/app-12345678.js"), timeout=5
        )
        with response:
            self.assertEqual(
                response.headers["Cache-Control"], "public, max-age=31536000, immutable"
            )

        # API 响应不干预缓存
        response = await asyncio.to_thread(
            urlopen,
            Request(f"{base_url}/api/web/ui", headers={"X-AETP-Token": AUTH_TOKEN}),
            timeout=5,
        )
        with response:
            self.assertIsNone(response.headers.get("Cache-Control"))


class CorsTests(unittest.IsolatedAsyncioTestCase):
    """webapi 的 CORS 支持（前端跨源切换后端地址的前提）。"""

    async def asyncSetUp(self) -> None:
        self.open_port: int = _free_port()
        self.off_port: int = _free_port()
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.open_context: Context = Context()
        self.off_context: Context = Context()
        self.addAsyncCleanup(unload_all, self.open_context)
        self.addAsyncCleanup(unload_all, self.off_context)

        open_config_path = Path(self.temp_dir.name) / "open.ini"
        open_config_path.write_text(
            "[web]\n" f"port = {self.open_port}\n" "auth_token = test-token\n" "cors_origins = *\n", encoding="utf-8"
        )
        off_config_path = Path(self.temp_dir.name) / "off.ini"
        off_config_path.write_text(
            "[web]\n"
            f"port = {self.off_port}\n"
            "auth_token = test-token\n",
            encoding="utf-8",
        )
        await self.open_context.plugin(
            AppConfigPlugin, {"path": str(open_config_path)}
        )
        await self.off_context.plugin(AppConfigPlugin, {"path": str(off_config_path)})
        await self.open_context.plugin(WebApiPlugin)
        await self.off_context.plugin(WebApiPlugin)

    async def test_allow_origin_and_preflight_with_wildcard(self) -> None:
        base_url = f"http://127.0.0.1:{self.open_port}"
        origin = {"Origin": "http://127.0.0.1:8080", "X-AETP-Token": AUTH_TOKEN}

        request = Request(f"{base_url}/api/web/routes", headers=origin)
        response = await asyncio.to_thread(urlopen, request, timeout=5)
        with response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")

        preflight = Request(
            f"{base_url}/api/web/routes",
            method="OPTIONS",
            headers={**origin, "Access-Control-Request-Method": "GET"},
        )
        response = await asyncio.to_thread(urlopen, preflight, timeout=5)
        with response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")

    async def test_default_cors_disabled(self) -> None:
        """未配置 cors_origins = 默认关闭跨源（收紧后语义，同源不受影响）。"""
        request = Request(
            f"http://127.0.0.1:{self.off_port}/api/web/routes",
            headers={"Origin": "http://127.0.0.1:8080", "X-AETP-Token": AUTH_TOKEN},
        )
        response = await asyncio.to_thread(urlopen, request, timeout=5)
        with response:
            self.assertIsNone(response.headers["Access-Control-Allow-Origin"])


class AuthTests(unittest.IsolatedAsyncioTestCase):
    """鉴权：令牌强制、401 信封、同源放行、静态白名单与身份注入。"""

    async def asyncSetUp(self) -> None:
        self.context: Context = Context()
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.port: int = _free_port()
        self.context.provide("node_kind", "master")
        config_path = Path(self.temp_dir.name) / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            f"install_root = {Path(self.temp_dir.name) / 'installed'}\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n",
            encoding="utf-8",
        )
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(WebApiPlugin)

        @self.context.webapi.get("/api/whoami", kind="api", name="test.whoami")
        def whoami(request: StarletteRequest) -> dict[str, object]:
            return {"auth": dict(getattr(request.state, "auth", {}))}

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_missing_or_wrong_token_rejected(self) -> None:
        base_url = f"http://127.0.0.1:{self.port}"
        for token in (None, "wrong-token"):
            status, _content_type, body = await asyncio.to_thread(
                _request, base_url, "/api/whoami", "GET", token
            )
            self.assertEqual(status, 401)
            self.assertIn("X-AETP-Token", json.loads(body)["error"])

    async def test_valid_token_passes_with_identity(self) -> None:
        base_url = f"http://127.0.0.1:{self.port}"
        status, _content_type, body = await asyncio.to_thread(
            _request, base_url, "/api/whoami"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["auth"], {"subject": "operator", "role": "admin"}
        )

    async def test_same_origin_browser_request_passes_without_token(self) -> None:
        """同源浏览器请求（Sec-Fetch-Site）免令牌——用户层登录/权限由用户管理插件叠加。"""
        base_url = f"http://127.0.0.1:{self.port}"
        status, _content_type, body = await asyncio.to_thread(
            _request,
            base_url,
            "/api/whoami",
            "GET",
            None,
            {"Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["auth"], {"subject": "operator", "role": "admin"}
        )

    async def test_static_paths_open_without_token(self) -> None:
        """非 /api 路径（SPA 页面 / 静态资源）免鉴权：浏览器导航带不了请求头。"""
        base_url = f"http://127.0.0.1:{self.port}"
        status, _content_type, _body = await asyncio.to_thread(
            _request, base_url, "/some/page", "GET", None
        )
        self.assertEqual(status, 404)  # 未鉴权也走到路由（而非 401）

    async def test_missing_auth_token_rejects_startup(self) -> None:
        """强制启用：[web] 缺 auth_token → 配置校验失败，节点起不来。"""
        context: Context = Context()
        context.provide("node_kind", "master")
        config_path = Path(self.temp_dir.name) / "notoken.ini"
        config_path.write_text(
            "[node]\nkind = master\n\n[web]\n" f"port = {_free_port()}\n",
            encoding="utf-8",
        )
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        with self.assertRaises((TypeError, ValueError)):
            await context.plugin(WebApiPlugin)
        await unload_all(context)


if __name__ == "__main__":
    unittest.main()
