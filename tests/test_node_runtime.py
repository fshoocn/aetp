from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path

from cordis_port import Context

from _helpers import free_port as _free_port
from _helpers import request as _request
from common.cordis_utils import unload_all
from common.node_runtime import (
    NodeKind,
    assemble_node,
    ensure_installed,
    load_node_profile,
    source_plugin_id,
)
from common.plugins.appconfig import AppConfig, AppConfigPlugin
from common.plugins.webapi import WebApiConfig, WebApiPlugin
from common.plugins.webapi.plugin_manager_plugin import PluginManagerPlugin


def _write_source(root: Path, plugin_id: str, *, broken: bool = False) -> Path:
    """造一个最小插件源目录（executor 风格：注册一条本节点 API）。

    ``broken=True`` 时构造抛错，用于验证「坏源不拖垮启动」。
    """
    source = root / plugin_id
    source.mkdir(parents=True)
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "1.0.0",
                "entrypoint": "plugin:DemoPlugin",
                "kind": "slave",
                "requires": ["can-bus"],
            }
        ),
        encoding="utf-8",
    )
    body = (
        "        raise RuntimeError('broken on purpose')\n"
        if broken
        else (
            f"        @ctx.webapi.get('/api/{plugin_id}', kind='api')\n"
            "        def status(request):\n"
            "            return {'active': True, 'node_kind': str(ctx.node_kind)}\n"
        )
    )
    (source / "plugin.py").write_text(
        (
            "class DemoPlugin:\n"
            f"    name = {plugin_id!r}\n"
            "    inject = ['webapi', 'node_kind']\n"
            "\n"
            "    def __init__(self, ctx, config):\n"
            f"{body}"
        ),
        encoding="utf-8",
    )
    return source


class NodeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context: Context | None = None
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.port: int = _free_port()
        self.base_url: str = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_assemble_slave_node_installs_sources_and_runs_headless(self) -> None:
        root = Path(self.temp_dir.name)
        source = _write_source(root, "executor-demo")
        self.assertEqual(source_plugin_id(source), "executor-demo")
        config_path = root / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = slave\n"
            "name = 测试从节点\n"
            "install_root = plugins\n"
            f"plugins = {source}\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n",
            encoding="utf-8",
        )

        self.context = await assemble_node(config_path=config_path)

        # 节点类型注册为 node_kind 服务，插件声明 inject 后可读
        self.assertEqual(self.context.node_kind, NodeKind.SLAVE)

        # 插件源已安装到本节点安装目录并启用，其 API 可用
        self.assertTrue((root / "plugins" / "executor-demo").is_dir())
        self.assertTrue((root / "plugins" / ".archives" / "executor-demo.zip").is_file())
        status, body = await asyncio.to_thread(_request, self.base_url, "/api/executor-demo")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["node_kind"], "slave")

        # 无头语义：没装 webui —— 页面路径 404，/api/web/ui 不存在
        self.assertEqual((await asyncio.to_thread(_request, self.base_url, "/some/page"))[0], 404)
        self.assertEqual((await asyncio.to_thread(_request, self.base_url, "/api/web/ui"))[0], 404)

        # 幂等：重复 ensure 不报错、不重复安装
        self.assertEqual(await ensure_installed(self.context, source), "executor-demo")
        plugins = [
            item
            for item in await self.context.plugins.list_plugins()
            if not item["builtin"]
        ]
        self.assertEqual([item["id"] for item in plugins], ["executor-demo"])

        # 归属元数据随安装保留（供未来的分发插件筛选）
        record = plugins[0]
        self.assertEqual(record["kind"], ["slave"])
        self.assertEqual(record["requires"], ["can-bus"])

        # 源内容变化（如重新构建）→ 内容摘要不同 → 自动换装，新代码生效
        plugin_file = source / "plugin.py"
        plugin_file.write_text(
            plugin_file.read_text(encoding="utf-8").replace(
                "'active': True", "'active': 'rebuilt'"
            ),
            encoding="utf-8",
        )
        self.assertEqual(await ensure_installed(self.context, source), "executor-demo")
        status, body = await asyncio.to_thread(_request, self.base_url, "/api/executor-demo")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["active"], "rebuilt")

    async def test_broken_preinstalled_source_does_not_block_startup(self) -> None:
        """单个预装插件失败不中断启动：其余照常装配，失败原因留档可重试。"""
        root = Path(self.temp_dir.name)
        broken = _write_source(root, "broken-demo", broken=True)
        good = _write_source(root, "good-demo")
        config_path = root / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = slave\n"
            "name = 测试从节点\n"
            "install_root = plugins\n"
            f"plugins = {broken}, {good}\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n",
            encoding="utf-8",
        )

        self.context = await assemble_node(config_path=config_path)

        # 坏源在前也不拖垮启动：好插件照常安装启用并对外服务
        status, _body = await asyncio.to_thread(_request, self.base_url, "/api/good-demo")
        self.assertEqual(status, 200)
        listing = {
            item["id"]: item for item in await self.context.plugins.list_plugins()
        }
        self.assertTrue(listing["good-demo"]["enabled"])
        self.assertFalse(listing["broken-demo"]["enabled"])
        self.assertIsNotNone(listing["broken-demo"]["last_error"])


class ZipDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """插件以 zip 包交付：source_plugin_id / ensure_installed 支持 zip。"""

    async def asyncSetUp(self) -> None:
        self.context: Context | None = None
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_install_from_zip_package(self) -> None:
        root = Path(self.temp_dir.name)
        source = _write_source(root, "executor-demo")
        package = root / "executor-demo.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source).as_posix())

        self.context = Context()
        self.context.provide("node_kind", NodeKind.SLAVE)
        config_path = root / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = slave\n"
            f"install_root = {root / 'plugins'}\n"
            "\n[web]\n"
            f"port = {_free_port()}\n"
            "auth_token = test-token\n",
            encoding="utf-8",
        )
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(PluginManagerPlugin)

        self.assertEqual(source_plugin_id(package), "executor-demo")
        self.assertEqual(await ensure_installed(self.context, package), "executor-demo")
        plugins = [
            item
            for item in await self.context.plugins.list_plugins()
            if not item["builtin"]
        ]
        self.assertEqual([item["id"] for item in plugins], ["executor-demo"])
        self.assertTrue((root / "plugins" / "executor-demo").is_dir())

        # 幂等：重复 ensure 不重复安装
        self.assertEqual(await ensure_installed(self.context, package), "executor-demo")
        plugins = [
            item
            for item in await self.context.plugins.list_plugins()
            if not item["builtin"]
        ]
        self.assertEqual([item["id"] for item in plugins], ["executor-demo"])


class NodeProfileTests(unittest.TestCase):
    """config.ini 的 [node] 节 → NodeProfile。"""

    def test_profile_reads_node_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.ini"
            path.write_text(
                "[node]\n"
                "kind = master\n"
                "name = 主节点\n"
                "plugins = sources/one, sources/two\n",
                encoding="utf-8",
            )
            profile = load_node_profile(AppConfig.from_file(path), base_dir=root)

        self.assertEqual(profile.kind, NodeKind.MASTER)
        self.assertEqual(profile.name, "主节点")
        self.assertEqual(
            profile.plugin_sources,
            (root / "sources/one", root / "sources/two"),
        )

    def test_defaults_invalid_kind_and_missing_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.ini"

            # 未写的键取默认：name 取本机设备名，install_root 取 plugins
            path.write_text("[node]\nkind = slave\n", encoding="utf-8")
            profile = load_node_profile(AppConfig.from_file(path), base_dir=root)
            self.assertEqual(profile.kind, NodeKind.SLAVE)
            self.assertEqual(profile.name, socket.gethostname())

            path.write_text("[node]\nkind = both\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_node_profile(AppConfig.from_file(path), base_dir=root)

            path.write_text("[node]\nname = x\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_node_profile(AppConfig.from_file(path), base_dir=root)

            path.write_text("[node]\nkind = slave\nextra = 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_node_profile(AppConfig.from_file(path), base_dir=root)

            path.write_text("[web]\nport = 1\n", encoding="utf-8")
            with self.assertRaises(KeyError):
                load_node_profile(AppConfig.from_file(path), base_dir=root)

    def test_web_config_adapter_converts_ini_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.ini"
            path.write_text(
                "[web]\n"
                "host = 0.0.0.0\n"
                "port = 9000\n"
                "access_log = true\n"
                "log_level = INFO\n"
                "start_timeout = 2.5\n"
                "auth_token = test-token\n"
                "cors_origins = http://a, http://b\n",
                encoding="utf-8",
            )
            web_config = WebApiConfig.from_ini(AppConfig.from_file(path).to_dict()["web"])

        self.assertEqual(
            web_config.to_dict(),
            {
                "host": "0.0.0.0",
                "port": 9000,
                "access_log": True,
                "log_level": "info",
                "start_timeout": 2.5,
                "auth_token": "***",
                "cors_origins": ("http://a", "http://b"),
            },
        )

        # 写法非法 → ValueError，指明字段与允许类型
        with self.assertRaisesRegex(ValueError, "监听端口 需要 int"):
            WebApiConfig.from_ini({"port": "abc"})
        with self.assertRaisesRegex(ValueError, "未知配置项"):
            WebApiConfig.from_ini({"nope": "1"})
        with self.assertRaisesRegex(ValueError, "cors_origins"):
            WebApiConfig.from_ini({"cors_origins": "ftp://x"})


if __name__ == "__main__":
    unittest.main()
