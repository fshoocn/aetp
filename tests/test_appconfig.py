"""appconfig：config.ini 解析（配置类）与 cordis 服务集成的测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cordis_port import Context

from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfig, AppConfigPlugin

_INI = """\
# 示例配置
[node]
name = master
enabled = true

[web]
port = 8080
ratio = 1.5
"""


def _write_ini(root: Path) -> Path:
    path = root / "config.ini"
    path.write_text(_INI, encoding="utf-8")
    return path


class AppConfigTests(unittest.TestCase):
    """config.ini → 配置类。"""

    def test_parse_sections_and_typed_getters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = AppConfig.from_file(_write_ini(Path(temporary)))
        self.assertEqual(config.sections(), ["node", "web"])
        self.assertEqual(config.get("node", "name"), "master")
        self.assertEqual(config.get_int("web", "port"), 8080)
        self.assertEqual(config.get_float("web", "ratio"), 1.5)
        self.assertIs(config.get_bool("node", "enabled"), True)
        self.assertEqual(config.get("node", "missing", "fallback"), "fallback")
        self.assertEqual(config.get("nosuch", "key", "x"), "x")

    def test_section_attribute_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = AppConfig.from_file(_write_ini(Path(temporary)))
        node = config.section("node")
        self.assertEqual(node.name, "master")
        self.assertEqual(node.section_name, "node")
        self.assertIn("name", node)
        self.assertEqual(node.to_dict()["name"], "master")

    def test_typed_getter_errors_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = AppConfig.from_file(_write_ini(root))
            with self.assertRaises(ValueError):
                config.get_int("node", "name")
            with self.assertRaises(ValueError):
                config.get_bool("web", "ratio")
            with self.assertRaises(FileNotFoundError):
                AppConfig.from_file(root / "missing.ini")
        self.assertEqual(AppConfig().get("web", "port", "8080"), "8080")


class AppConfigPluginTests(unittest.IsolatedAsyncioTestCase):
    """cordis 集成：ctx.appconfig 服务。"""

    async def asyncSetUp(self) -> None:
        self.context: Context | None = None
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_service_reads_ini(self) -> None:
        path = _write_ini(Path(self.temp_dir.name))
        self.context = Context()
        await self.context.plugin(AppConfigPlugin, {"path": str(path)})

        appconfig = self.context.appconfig
        self.assertEqual(appconfig.sections(), ["node", "web"])
        self.assertEqual(appconfig.get_int("web", "port"), 8080)
        self.assertEqual(appconfig.to_dict()["node"]["name"], "master")
        self.assertEqual(appconfig.path_loaded, path.resolve())

    async def test_missing_file_starts_with_empty_config(self) -> None:
        self.context = Context()
        missing = Path(self.temp_dir.name) / "missing.ini"
        await self.context.plugin(AppConfigPlugin, {"path": str(missing)})

        appconfig = self.context.appconfig
        self.assertEqual(appconfig.sections(), [])
        self.assertIsNone(appconfig.path_loaded)
        self.assertEqual(appconfig.get("web", "port", "8080"), "8080")
