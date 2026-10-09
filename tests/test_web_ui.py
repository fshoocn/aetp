from __future__ import annotations

import json
import socket
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cordis_port import Context
from starlette.requests import Request as StarletteRequest

from common.cordis_utils import unload_all
from common.plugins.webapi import WebApiPlugin
from common.plugins.webapi.plugin_manager_plugin import PluginManagerPlugin
from common.plugins.webapi.router import RouteRegistry
from common.plugins.webapi.types import RouteRecord

_SOURCE_ROOT: Path = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(base_url: str, path: str, method: str = "GET") -> tuple[int, str, bytes]:
    request = Request(f"{base_url}{path}", method=method)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    if response is None:
        raise RuntimeError("HTTP 请求没有返回响应")
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
        await self.context.plugin(WebApiPlugin, {"port": self.port})
        await self.context.plugin(
            PluginManagerPlugin,
            {"install_root": str(Path(self.temp_dir.name) / "installed")},
        )
        plugins = self.context.plugins
        await plugins.install_source(_SOURCE_ROOT / "masterplugins" / "webui")
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

            status, content_type, body = _request(base_url, "/api/web/ui")
            self.assertEqual(status, 200)
            self.assertIn("application/json", content_type)
            manifest = json.loads(body)
            self.assertEqual(len(manifest), 1)
            self.assertEqual(manifest[0]["owner"], "test-ui")
            self.assertEqual(manifest[0]["path"], "/plugins/test-ui")

            status, _, body = _request(base_url, manifest[0]["entry"])
            self.assertEqual(status, 200)
            self.assertEqual(body.decode("utf-8"), component_source)

            status, content_type, body = _request(base_url, "/plugins/test-ui")
            self.assertEqual(status, 200)
            self.assertIn("text/html", content_type)
            self.assertIn(b"/ui-static/assets/", body)

            self.assertEqual(_request(base_url, "/api/missing")[0], 404)
            self.assertEqual(_request(base_url, "/ui-static/missing.js")[0], 404)

            await fiber.dispose()
            status, _, body = _request(base_url, "/api/web/ui")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), [])
            self.assertEqual(_request(base_url, manifest[0]["entry"])[0], 404)


class CorsTests(unittest.IsolatedAsyncioTestCase):
    """webapi 的 CORS 支持（前端跨源切换后端地址的前提）。"""

    async def asyncSetUp(self) -> None:
        self.open_port: int = _free_port()
        self.off_port: int = _free_port()
        self.open_context: Context = Context()
        self.off_context: Context = Context()
        self.addAsyncCleanup(unload_all, self.open_context)
        self.addAsyncCleanup(unload_all, self.off_context)
        await self.open_context.plugin(WebApiPlugin, {"port": self.open_port})
        await self.off_context.plugin(
            WebApiPlugin, {"port": self.off_port, "cors_origins": []}
        )

    def test_allow_origin_and_preflight_by_default(self) -> None:
        base_url = f"http://127.0.0.1:{self.open_port}"
        origin = {"Origin": "http://127.0.0.1:8080"}

        request = Request(f"{base_url}/api/web/routes", headers=origin)
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")

        preflight = Request(
            f"{base_url}/api/web/routes",
            method="OPTIONS",
            headers={**origin, "Access-Control-Request-Method": "GET"},
        )
        with urlopen(preflight, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")

    def test_disabled_when_origins_empty(self) -> None:
        request = Request(
            f"http://127.0.0.1:{self.off_port}/api/web/routes",
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        with urlopen(request, timeout=5) as response:
            self.assertIsNone(response.headers["Access-Control-Allow-Origin"])


if __name__ == "__main__":
    unittest.main()