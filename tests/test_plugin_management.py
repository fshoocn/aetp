from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import types as module_types
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cordis_port import Context

from common.cordis_utils import unload_all
from master.plugins.webapi import WebApiPlugin
from master.plugins.webapi.plugin_manager import PluginManager, PluginManagerError
from master.plugins.webui import WebUiPlugin


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    headers = {"Content-Type": content_type} if content_type else {}
    request = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
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
            raise RuntimeError("HTTP 响应的状态码或正文类型无效")  # noqa: TRY004
        return status, body


async def _arequest(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    return await asyncio.to_thread(
        _request,
        base_url,
        path,
        method=method,
        body=body,
        content_type=content_type,
    )


def _plugin_archive(plugin_id: str = "uploaded-sample", plugin_class: str = "UploadedPlugin") -> bytes:
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr(
            "plugin.json",
            json.dumps(
                {
                    "id": plugin_id,
                    "name": plugin_id,
                    "version": "1.0.0",
                    "entrypoint": f"plugin:{plugin_class}",
                    "resource_root": "ui",
                }
            ),
        )
        package.writestr(
            "plugin.py",
            (
                f"class {plugin_class}:\n"
                f"    name = {plugin_id!r}\n"
                "    inject = ['webapi', 'webui']\n"
                "\n"
                "    def __init__(self, ctx, config):\n"
                "        ctx.webui.register_ui(\n"
                "            'home', kind='page', format='vue', resource_root=config['resource_root'],\n"
                f"            entry='Home.vue', path='/plugins/{plugin_id}', title={plugin_id!r},\n"
                "        )\n"
                "\n"
                f"        @ctx.webapi.get('/api/{plugin_id}', kind='api')\n"
                "        def status(request):\n"
                "            return {'active': True}\n"
            ),
        )
        package.writestr("ui/Home.vue", "<template><h1>Uploaded Sample</h1></template>")
    return archive.getvalue()


class PluginManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context: Context = Context()
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.port: int = _free_port()
        self.base_url: str = f"http://127.0.0.1:{self.port}"
        await self.context.plugin(
            WebApiPlugin,
            {
                "port": self.port,
                "plugin_install_root": str(Path(self.temp_dir.name) / "installed"),
            },
        )
        await self.context.plugin(WebUiPlugin)

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_upload_enable_disable_and_uninstall(self) -> None:
        status, body = await _arequest(
            self.base_url,
            "/api/plugins/upload",
            method="POST",
            body=_plugin_archive(),
            content_type="application/zip",
        )
        self.assertEqual(status, 200)
        uploaded = json.loads(body)["plugin"]
        self.assertEqual(uploaded["id"], "uploaded-sample")
        self.assertFalse(uploaded["enabled"])

        status, body = await _arequest(self.base_url, "/api/plugins")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["plugins"]), 1)

        status, body = await _arequest(self.base_url, "/api/plugins/uploaded-sample/enable", method="POST")
        self.assertEqual(status, 200, body.decode("utf-8"))
        self.assertTrue(json.loads(body)["plugin"]["enabled"])
        self.assertEqual((await _arequest(self.base_url, "/api/uploaded-sample"))[0], 200)

        status, body = await _arequest(self.base_url, "/api/web/ui")
        self.assertEqual(status, 200)
        contribution = next(item for item in json.loads(body) if item["owner"] == "uploaded-sample")
        self.assertEqual((await _arequest(self.base_url, contribution["entry"]))[0], 200)

        status, body = await _arequest(self.base_url, "/api/plugins/uploaded-sample/disable", method="POST")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["plugin"]["enabled"])
        self.assertEqual((await _arequest(self.base_url, "/api/uploaded-sample"))[0], 404)
        ui_status, ui_body = await _arequest(self.base_url, "/api/web/ui")
        self.assertEqual(ui_status, 200)
        self.assertFalse(any(item["owner"] == "uploaded-sample" for item in json.loads(ui_body)))

        status, body = await _arequest(self.base_url, "/api/plugins/uploaded-sample/enable", method="POST")
        self.assertEqual(status, 200, body.decode("utf-8"))
        self.assertEqual((await _arequest(self.base_url, "/api/uploaded-sample"))[0], 200)

        status, body = await _arequest(self.base_url, "/api/plugins/uploaded-sample", method="DELETE")
        self.assertEqual(status, 200)
        _, plugins_body = await _arequest(self.base_url, "/api/plugins")
        self.assertEqual(json.loads(plugins_body)["plugins"], [])
        _, ui_body = await _arequest(self.base_url, "/api/web/ui")
        self.assertFalse(any(item["owner"] == "uploaded-sample" for item in json.loads(ui_body)))

    async def test_similar_plugin_ids_keep_isolated_modules(self) -> None:
        plugins = (
            ("same-id", "HyphenPlugin"),
            ("same_id", "UnderscorePlugin"),
        )
        for plugin_id, plugin_class in plugins:
            status, body = await _arequest(
                self.base_url,
                "/api/plugins/upload",
                method="POST",
                body=_plugin_archive(plugin_id, plugin_class),
                content_type="application/zip",
            )
            self.assertEqual(status, 200, body.decode("utf-8"))

        for plugin_id, _ in plugins:
            status, body = await _arequest(
                self.base_url,
                f"/api/plugins/{plugin_id}/enable",
                method="POST",
            )
            self.assertEqual(status, 200, body.decode("utf-8"))
            self.assertEqual((await _arequest(self.base_url, f"/api/{plugin_id}"))[0], 200)

        status, body = await _arequest(
            self.base_url,
            "/api/plugins/same-id/disable",
            method="POST",
        )
        self.assertEqual(status, 200, body.decode("utf-8"))
        self.assertEqual((await _arequest(self.base_url, "/api/same-id"))[0], 404)
        self.assertEqual((await _arequest(self.base_url, "/api/same_id"))[0], 200)

    async def test_upload_rejects_zip_path_traversal(self) -> None:
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("../outside.txt", "nope")
            package.writestr("plugin.json", "{}")

        status, body = await _arequest(
            self.base_url,
            "/api/plugins/upload",
            method="POST",
            body=archive.getvalue(),
            content_type="application/zip",
        )
        self.assertEqual(status, 400)
        self.assertIn("非法路径", json.loads(body)["error"])

    async def test_registry_write_failure_rolls_back_install(self) -> None:
        class RegistryWriteFailureManager(PluginManager):
            def _write_registry(self) -> None:
                raise OSError("simulated registry failure")

        install_root = Path(self.temp_dir.name) / "failed-install"
        manager = RegistryWriteFailureManager(self.context, asyncio.get_running_loop(), install_root)
        with self.assertRaises(PluginManagerError):
            await manager.install_archive(_plugin_archive())

        self.assertEqual(manager.records, {})
        self.assertFalse((install_root / "uploaded-sample").exists())
        self.assertEqual(list(install_root.iterdir()), [])

    async def test_enable_registry_failure_disposes_plugin_fiber(self) -> None:
        class FailEnabledWriteManager(PluginManager):
            def __init__(self, ctx: Context, loop: asyncio.AbstractEventLoop, root: Path) -> None:
                super().__init__(ctx, loop, root)
                self.failed: bool = False

            def _write_registry(self) -> None:
                record = self.records.get(plugin_id)
                if record is not None and record["enabled"] and not self.failed:
                    self.failed = True
                    raise OSError("simulated registry failure")
                super()._write_registry()

        install_root = Path(self.temp_dir.name) / "enable-failure"
        plugin_id = "enable-failure"
        manager = FailEnabledWriteManager(self.context, asyncio.get_running_loop(), install_root)
        await manager.install_archive(_plugin_archive(plugin_id, "EnableFailurePlugin"))
        with self.assertRaises(PluginManagerError):
            await manager.enable(plugin_id)

        self.assertFalse(manager.records[plugin_id]["enabled"])
        self.assertNotIn(plugin_id, manager.fibers)
        self.assertEqual((await _arequest(self.base_url, f"/api/{plugin_id}"))[0], 404)

    async def test_concurrent_enable_is_idempotent(self) -> None:
        init_calls = 0

        class ConcurrentPlugin:
            name: str = "concurrent-enable-test"
            inject: tuple[str, ...] = ("webapi",)

            def __init__(self, ctx: Context, config: object) -> None:
                del ctx, config

            async def init(self) -> None:
                nonlocal init_calls
                init_calls += 1
                await asyncio.sleep(0.01)

        class ConcurrentPluginManager(PluginManager):
            def _load_plugin_class(
                self, package_root: Path, package_name: str, entrypoint: str
            ) -> type[object]:
                del package_root, package_name, entrypoint
                return ConcurrentPlugin

        install_root = Path(self.temp_dir.name) / "concurrent-enable"
        manager = ConcurrentPluginManager(self.context, asyncio.get_running_loop(), install_root)
        plugin_id = "concurrent-enable"
        await manager.install_archive(_plugin_archive(plugin_id, "ConcurrentPlugin"))
        results = await asyncio.gather(manager.enable(plugin_id), manager.enable(plugin_id))

        self.assertEqual(init_calls, 1)
        self.assertTrue(all(item["enabled"] for item in results))
        self.assertEqual(list(manager.fibers), [plugin_id])
        await manager.disable(plugin_id)

    async def test_cancelled_enable_disposes_plugin_fiber(self) -> None:
        init_started = asyncio.Event()
        continue_init = asyncio.Event()

        class BlockingPlugin:
            name: str = "cancelled-enable-test"
            inject: tuple[str, ...] = ("webapi",)

            def __init__(self, ctx: Context, config: object) -> None:
                del ctx, config

            async def init(self) -> None:
                init_started.set()
                await continue_init.wait()

        class BlockingPluginManager(PluginManager):
            def _load_plugin_class(
                self, package_root: Path, package_name: str, entrypoint: str
            ) -> type[object]:
                del package_root, entrypoint
                sys.modules[package_name] = module_types.ModuleType(package_name)
                sys.modules[f"{package_name}.plugin"] = module_types.ModuleType(
                    f"{package_name}.plugin"
                )
                return BlockingPlugin

        install_root = Path(self.temp_dir.name) / "cancelled-enable"
        manager = BlockingPluginManager(self.context, asyncio.get_running_loop(), install_root)
        plugin_id = "cancelled-enable"
        await manager.install_archive(_plugin_archive(plugin_id, "BlockingPlugin"))

        module_name = f"_aetp_plugin_{plugin_id.encode('ascii').hex()}"

        enable_task = asyncio.create_task(manager.enable(plugin_id))
        await asyncio.wait_for(init_started.wait(), timeout=2)
        enable_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await enable_task

        self.assertFalse(manager.records[plugin_id]["enabled"])
        self.assertNotIn(plugin_id, manager.fibers)
        self.assertEqual((await _arequest(self.base_url, f"/api/{plugin_id}"))[0], 404)
        self.assertFalse(
            any(name == module_name or name.startswith(module_name + ".") for name in sys.modules)
        )


if __name__ == "__main__":
    unittest.main()