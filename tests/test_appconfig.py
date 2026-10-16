"""appconfig：config.ini 解析（配置类）与 cordis 服务集成的测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from cordis_port import Context

from common.config_base import FieldConfig
from common.cordis_utils import unload_all
from common.plugins.appconfig import AppConfig, AppConfigPlugin, write_ini_values

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


class DemoConfig(FieldConfig):
    """测试用配置模型：声明 ``ini_section`` 接入注入 / 写回钩子。"""

    ini_section: ClassVar[str | None] = "demo"
    _FIELDS: ClassVar[dict[str, tuple[tuple[type[object], ...], str]]] = {
        "host": ((str,), "host（监听地址）"),
        "port": ((int,), "port（监听端口）"),
    }

    host: str = "127.0.0.1"
    port: int = 80


class DemoPlugin:
    """测试用插件：记录每次构造（含 update 重载）拿到的解析后配置。"""

    name: ClassVar[str] = "demo"
    inject: ClassVar[list[str]] = ["appconfig"]
    Config: ClassVar[type[DemoConfig]] = DemoConfig
    #: 每次构造拿到的解析后配置（新到旧）
    seen: ClassVar[list[DemoConfig]] = []

    def __init__(self, ctx: Context, config: DemoConfig | None = None) -> None:
        self.config: DemoConfig = (
            config if isinstance(config, DemoConfig) else DemoConfig()
        )
        type(self).seen.append(self.config)


class PlainPlugin:
    """测试用插件：未声明 ``Config``，配置应原样透传。"""

    name: ClassVar[str] = "plain"
    inject: ClassVar[list[str]] = ["appconfig"]

    def __init__(self, ctx: Context, config: object | None = None) -> None:
        self.config: object | None = config


class FieldConfigTests(unittest.TestCase):
    """字段表驱动的默认值 / 校验 / ini 往返 / 层合并。"""

    def test_ini_roundtrip_and_validation(self) -> None:
        config = DemoConfig.from_ini({"port": "9000"})
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(DemoConfig.to_ini({"port": 9000}), {"port": "9000"})
        self.assertEqual(
            DemoConfig.to_ini(config), {"host": "127.0.0.1", "port": "9000"}
        )
        with self.assertRaisesRegex(ValueError, "port（监听端口） 需要 int"):
            DemoConfig.from_ini({"port": "abc"})
        with self.assertRaisesRegex(ValueError, "未知配置项"):
            DemoConfig.from_ini({"nope": "1"})

    def test_to_ini_rejects_invalid_input(self) -> None:
        """非法输入显式抛 TypeError，不静默返回空字典。"""
        with self.assertRaises(TypeError):
            DemoConfig.to_ini(object())
        self.assertEqual(DemoConfig.to_ini(None), {})

    def test_merge_layers_last_wins(self) -> None:
        merged = DemoConfig.merge({"port": 1}, {"host": "outer"})
        self.assertEqual(merged.port, 1)
        self.assertEqual(merged.host, "outer")
        self.assertEqual(DemoConfig.merge(None, DemoConfig(port=2)).port, 2)


class WriteIniValuesTests(unittest.TestCase):
    """外科手术式写回：只改点名的键，注释与排版保留。"""

    def test_replace_insert_and_create_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.ini"
            path.write_text(
                "# 顶部注释\n[node]\nname = master\n\n[web]\n# 端口说明\nport = 8080\n",
                encoding="utf-8",
            )
            write_ini_values(
                path,
                {
                    "web": {"port": "9000", "host": "0.0.0.0"},
                    "new": {"k": "v"},
                },
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("# 顶部注释", text)
        self.assertIn("# 端口说明", text)
        self.assertIn("name = master", text)
        self.assertIn("port = 9000", text)
        self.assertNotIn("8080", text)
        # 新键插到节尾（下个节之前），不在文件尾
        self.assertLess(text.index("host = 0.0.0.0"), text.index("[new]"))
        self.assertIn("[new]", text)
        self.assertIn("k = v", text)

    def test_update_section_keeps_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = _write_ini(Path(temporary))
            config = AppConfig.from_file(path)
            config.update_section("web", {"port": "9090"})
            self.assertEqual(config.get("web", "port"), "9090")
            text = path.read_text(encoding="utf-8")
        self.assertIn("# 示例配置", text)
        self.assertIn("port = 9090", text)
        self.assertNotIn("8080", text)

    def test_update_section_requires_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "需要显式传入 path"):
            AppConfig().update_section("web", {"port": "1"})

    def test_strips_bom_and_leaves_no_temporary(self) -> None:
        """带 BOM 的文件剥 BOM 后按节更新（不重复追加节）；写完不留临时文件。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.ini"
            path.write_bytes("[node]\nname = 从节点\n".encode("utf-8-sig"))
            write_ini_values(path, {"node": {"name": "新名字"}})
            text = path.read_text(encoding="utf-8")
            self.assertIn("name = 新名字", text)
            self.assertNotIn("\ufeff", text)
            self.assertEqual(text.count("[node]"), 1)
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])


class ConfigBridgeTests(unittest.IsolatedAsyncioTestCase):
    """appconfig 的两个配置生命周期钩子：注入默认值 / 提交写回。"""

    async def asyncSetUp(self) -> None:
        self.context: Context | None = None
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        DemoPlugin.seen = []

    async def asyncTearDown(self) -> None:
        if self.context is not None:
            await unload_all(self.context)
        self.temp_dir.cleanup()

    def _write_bridge_ini(self) -> Path:
        path = Path(self.temp_dir.name) / "config.ini"
        path.write_text(
            "# 顶部注释\n[demo]\n# 端口说明\nport = 9000\n", encoding="utf-8"
        )
        return path

    async def test_injects_ini_defaults(self) -> None:
        path = self._write_bridge_ini()
        self.context = Context()
        await self.context.plugin(AppConfigPlugin, {"path": str(path)})
        await self.context.plugin(DemoPlugin)

        config = DemoPlugin.seen[-1]
        self.assertEqual(config.port, 9000)  # ini 节值覆盖类默认值
        self.assertEqual(config.host, "127.0.0.1")  # 未给的键取类默认值

    async def test_explicit_config_overrides_ini(self) -> None:
        path = self._write_bridge_ini()
        self.context = Context()
        await self.context.plugin(AppConfigPlugin, {"path": str(path)})
        await self.context.plugin(DemoPlugin, {"port": 7000})

        config = DemoPlugin.seen[-1]
        self.assertEqual(config.port, 7000)  # 显式传入覆盖 ini
        self.assertEqual(config.host, "127.0.0.1")

    async def test_update_writes_back_ini(self) -> None:
        path = self._write_bridge_ini()
        self.context = Context()
        await self.context.plugin(AppConfigPlugin, {"path": str(path)})
        fiber = await self.context.plugin(DemoPlugin)

        await fiber.ctx.update({"port": 7100})
        self.assertEqual(DemoPlugin.seen[-1].port, 7100)  # 运行时已更新
        self.assertEqual(AppConfig.from_file(path).get("demo", "port"), "7100")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# 顶部注释", text)  # 注释保留
        self.assertIn("# 端口说明", text)

    async def test_no_save_skips_write_back(self) -> None:
        path = self._write_bridge_ini()
        self.context = Context()
        await self.context.plugin(AppConfigPlugin, {"path": str(path)})
        fiber = await self.context.plugin(DemoPlugin)

        await fiber.ctx.update({"port": 7200}, no_save=True)
        self.assertEqual(DemoPlugin.seen[-1].port, 7200)  # 运行时已更新
        self.assertEqual(AppConfig.from_file(path).get("demo", "port"), "9000")

    async def test_plugin_without_schema_is_untouched(self) -> None:
        path = self._write_bridge_ini()
        self.context = Context()
        await self.context.plugin(AppConfigPlugin, {"path": str(path)})
        plain = await self.context.plugin(PlainPlugin, {"x": 1})

        self.assertEqual(plain.config, {"x": 1})  # 原样透传，不注入不校验
