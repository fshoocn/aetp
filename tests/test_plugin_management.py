from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import struct
import sys
import tempfile
import types as module_types
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from typing import cast

from cordis_port import Context
from cordis_port.logger import Exporter, Logger, Message

from _helpers import arequest as _arequest
from _helpers import free_port as _free_port
from common.cordis_utils import unload_all
from common.node_runtime import ensure_installed
from common.plugins.appconfig import AppConfigPlugin
from common.plugins.db import DbPlugin
from common.plugins.db.registry import PluginRecordStore
from common.plugins.webapi import WebApiPlugin
from common.plugins.webapi.plugin_manager import PluginManager, PluginManagerError, builtin_specs
from common.plugins.webapi.plugin_manager_plugin import (
    MAX_CONFIG_BODY_BYTES,
    PluginManagerPlugin,
)

_SOURCE_ROOT: Path = Path(__file__).resolve().parents[1]

#: test_upload_strips_bytecode_cache 用的最小合法清单
_MANIFEST_BYTECODE: dict[str, object] = {
    "id": "bytecode-sample",
    "name": "bytecode-sample",
    "version": "1.0.0",
    "entrypoint": "plugin:BytecodePlugin",
    "kind": "master",
}


def _plugin_archive(
    plugin_id: str = "uploaded-sample",
    plugin_class: str = "UploadedPlugin",
    *,
    requires: list[str] | None = None,
    kind: str | list[str] | None = "master",
    version: str = "1.0.0",
    broken: bool = False,
    readme: str | None = None,
    inject: list[str] | None = None,
) -> bytes:
    manifest: dict[str, object] = {
        "id": plugin_id,
        "name": plugin_id,
        "version": version,
        "entrypoint": f"plugin:{plugin_class}",
        "resource_root": "ui",
    }
    if requires is not None:
        manifest["requires"] = requires
    if kind is not None:
        manifest["kind"] = kind
    inject_expr = repr(inject if inject is not None else ["webapi", "webui"])
    init_body = (
        "        raise RuntimeError('broken on purpose')\n"
        if broken
        else (
            "        ctx.webui.register_ui(\n"
            "            'home', kind='page', format='vue', resource_root=config['resource_root'],\n"
            f"            entry='Home.vue', path='/plugins/{plugin_id}', title={plugin_id!r},\n"
            "        )\n"
            "\n"
            f"        @ctx.webapi.get('/api/{plugin_id}', kind='api')\n"
            "        def status(request):\n"
            f"            return {{'active': True, 'version': {version!r}}}\n"
        )
    )
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("plugin.json", json.dumps(manifest))
        package.writestr(
            "plugin.py",
            (
                f"class {plugin_class}:\n"
                f"    name = {plugin_id!r}\n"
                f"    inject = {inject_expr}\n"
                "\n"
                "    def __init__(self, ctx, config):\n"
                f"{init_body}"
            ),
        )
        package.writestr("ui/Home.vue", "<template><h1>Uploaded Sample</h1></template>")
        if readme is not None:
            package.writestr("README.md", readme)
    return archive.getvalue()


def _patch_member_declared_size(
    archive: bytes, member_name: str, fake_size: int
) -> bytes:
    """把 zip 成员的声明未压缩大小改成 ``fake_size``（模拟谎报尺寸的篡改包）。

    本地头与中央目录两处都改，避免 zipfile 直接报头不一致。
    """
    data = bytearray(archive)
    with zipfile.ZipFile(BytesIO(archive)) as package:
        header_offset = package.getinfo(member_name).header_offset
    struct.pack_into("<I", data, header_offset + 22, fake_size)
    index = data.rfind(b"PK\x01\x02")
    while index >= 0:
        name_len = struct.unpack_from("<H", data, index + 28)[0]
        name = bytes(data[index + 46 : index + 46 + name_len]).decode("utf-8")
        if name == member_name:
            struct.pack_into("<I", data, index + 24, fake_size)
            return bytes(data)
        index = data.rfind(b"PK\x01\x02", 0, index)
    raise AssertionError(f"zip 里没有成员 {member_name}")


def _config_plugin_archive(plugin_id: str = "config-sample") -> bytes:
    """带 ``FieldConfig`` schema（``ini_section = "sample"``）的测试插件包。

    ``port == 6666`` 时构造抛错，用于验证「配置已保存但插件重载失败」。
    """
    manifest: dict[str, object] = {
        "id": plugin_id,
        "name": plugin_id,
        "version": "1.0.0",
        "entrypoint": "plugin:ConfigPlugin",
        "kind": "master",
    }
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("plugin.json", json.dumps(manifest))
        package.writestr(
            "plugin.py",
            (
                "from common.config_base import FieldConfig\n"
                "\n"
                "\n"
                "class SampleConfig(FieldConfig):\n"
                "    ini_section = 'sample'\n"
                "    _FIELDS = {\n"
                "        'port': ((int,), 'port（监听端口）'),\n"
                "        'host': ((str,), 'host（监听地址）'),\n"
                "    }\n"
                "    port: int = 80\n"
                "    host: str = '127.0.0.1'\n"
                "\n"
                "\n"
                "class ConfigPlugin:\n"
                f"    name = {plugin_id!r}\n"
                "    inject = ['appconfig']\n"
                "    Config = SampleConfig\n"
                "\n"
                "    def __init__(self, ctx, config):\n"
                "        del ctx\n"
                "        self.config = (\n"
                "            config if isinstance(config, SampleConfig) else SampleConfig()\n"
                "        )\n"
                "        if self.config.port == 6666:\n"
                "            raise RuntimeError('bad port on purpose')\n"
            ),
        )
    return archive.getvalue()


class PluginManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context: Context = Context()
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.port: int = _free_port()
        self.base_url: str = f"http://127.0.0.1:{self.port}"
        self.context.provide("node_kind", "master")
        config_path = Path(self.temp_dir.name) / "config.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            f"install_root = {Path(self.temp_dir.name) / 'installed'}\n"
            f"plugins = {_SOURCE_ROOT / 'master' / 'webui'}\n"
            "\n[web]\n"
            f"port = {self.port}\n"
            "auth_token = test-token\n"
            "\n[db]\n"
            f"url = sqlite+aiosqlite:///{(Path(self.temp_dir.name) / 'aetp.db').as_posix()}\n",
            encoding="utf-8",
        )
        await self.context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await self.context.plugin(DbPlugin)
        await self.context.plugin(WebApiPlugin)
        await self.context.plugin(PluginManagerPlugin)
        plugins = self.context.plugins
        await plugins.install_source(_SOURCE_ROOT / "master" / "webui")
        await plugins.enable("webui")

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_install_validates_node_kind(self) -> None:
        """安装前校验目标 kind 覆盖当前节点（准入门槛），校验失败不留数据。"""
        plugins = self.context.plugins  # asyncSetUp 已提供 node_kind=master

        # any 不限；单值一致；列表含当前节点（主从通用）→ 允许
        record = await plugins.install_archive(_plugin_archive("k-any", kind="any"))
        self.assertEqual(record["kind"], ["any"])
        record = await plugins.install_archive(_plugin_archive("k-master", kind="master"))
        self.assertEqual(record["kind"], ["master"])
        record = await plugins.install_archive(
            _plugin_archive("k-both", kind=["master", "slave"])
        )
        self.assertEqual(record["kind"], ["master", "slave"])

        # 不支持当前节点 / 非法值 / 未声明 / any 混写 → 拒绝
        rejected_archives = [
            _plugin_archive("k-slave", kind="slave"),
            _plugin_archive("k-bad", kind="both"),
            _plugin_archive("k-missing", kind=None),
            _plugin_archive("k-mixed", kind=["any", "master"]),
        ]
        for archive in rejected_archives:
            with self.assertRaises(PluginManagerError):
                await plugins.install_archive(archive)

        # 没有 node_kind 服务的环境：无法校验即拒绝
        bare: Context = Context()
        bare_config_path = Path(self.temp_dir.name) / "bare.ini"
        bare_config_path.write_text(
            "[node]\n"
            "kind = master\n"
            f"install_root = {Path(self.temp_dir.name) / 'bare'}\n"
            "\n[web]\n"
            f"port = {_free_port()}\n"
            "auth_token = test-token\n",
            encoding="utf-8",
        )
        await bare.plugin(AppConfigPlugin, {"path": str(bare_config_path)})
        await bare.plugin(WebApiPlugin)
        await bare.plugin(PluginManagerPlugin)
        with self.assertRaises(PluginManagerError):
            await bare.plugins.install_archive(_plugin_archive("k-bare"))
        await unload_all(bare)

        # 校验失败不留数据：无暂存目录、无插件目录、无归档、注册表无残留
        installed_root = Path(self.temp_dir.name) / "installed"
        self.assertEqual(list(installed_root.glob(".upload-*")), [])
        for rejected in ("k-slave", "k-bad", "k-missing", "k-mixed"):
            self.assertFalse((installed_root / rejected).exists())
            self.assertFalse((installed_root / ".archives" / f"{rejected}.zip").exists())
        ids = sorted(item["id"] for item in await plugins.list_plugins())
        self.assertEqual(
            ids,
            [
                "appconfig",
                "db",
                "k-any",
                "k-both",
                "k-master",
                "plugin-manager",
                "webapi",
                "webui",
            ],
        )

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
        uploaded_list = [
            item
            for item in json.loads(body)["plugins"]
            if item["id"] != "webui" and not item["builtin"]
        ]
        self.assertEqual(len(uploaded_list), 1)

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
        remaining = [
            item
            for item in json.loads(plugins_body)["plugins"]
            if item["id"] != "webui" and not item["builtin"]
        ]
        self.assertEqual(remaining, [])
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

    async def test_kind_requires_metadata_and_package_service(self) -> None:
        archive = _plugin_archive(
            "both-sample",
            "BothPlugin",
            kind=["master", "slave"],
            requires=["can-bus", "iso-tp"],
        )
        status, body = await _arequest(
            self.base_url,
            "/api/plugins/upload",
            method="POST",
            body=archive,
            content_type="application/zip",
        )
        self.assertEqual(status, 200)
        plugin = json.loads(body)["plugin"]
        self.assertEqual(plugin["kind"], ["master", "slave"])
        self.assertEqual(plugin["requires"], ["can-bus", "iso-tp"])

        # requires 缺省为空列表；kind 单值归一化为列表
        status, body = await _arequest(
            self.base_url,
            "/api/plugins/upload",
            method="POST",
            body=_plugin_archive("plain-sample", "PlainPlugin"),
            content_type="application/zip",
        )
        self.assertEqual(status, 200)
        plain = json.loads(body)["plugin"]
        self.assertEqual(plain["kind"], ["master"])
        self.assertEqual(plain["requires"], [])

        # ctx.plugins 服务：未来的主从通信插件走这里读清单 / 取原始包
        service = self.context.plugins
        listing = await service.list_plugins()
        record = next(item for item in listing if item["id"] == "both-sample")
        self.assertEqual(record["kind"], ["master", "slave"])
        self.assertEqual(await service.package_bytes("both-sample"), archive)

        await service.uninstall("both-sample")
        with self.assertRaises(PluginManagerError):
            await service.package_bytes("both-sample")

    async def test_plugin_readme_documented_and_served(self) -> None:
        """描述文件约定：包根 README.md；列表带 has_readme，接口返回原文。"""
        readme = "# 示例插件\n\n功能：演示。\n\n能力接口：`GET /api/example`。\n"
        plugins = self.context.plugins
        record = await plugins.install_archive(
            _plugin_archive("doc-sample", "DocPlugin", readme=readme), replace=True
        )
        self.assertTrue(record["has_readme"])
        self.assertEqual(await plugins.plugin_readme("doc-sample"), readme)

        status, body = await _arequest(
            self.base_url, "/api/plugins/doc-sample/readme"
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"id": "doc-sample", "content": readme})

    async def test_plugin_without_readme_reports_absent(self) -> None:
        """无 README.md：has_readme 为假，接口 404（前端据此隐藏「文档」入口）。"""
        plugins = self.context.plugins
        record = await plugins.install_archive(
            _plugin_archive("nodoc-sample", "NoDocPlugin", readme=None), replace=True
        )
        self.assertFalse(record["has_readme"])

        status, body = await _arequest(
            self.base_url, "/api/plugins/nodoc-sample/readme"
        )
        self.assertEqual(status, 404)
        self.assertIn("README", json.loads(body)["error"])

    async def test_plugin_readme_rejects_oversized_and_unknown(self) -> None:
        """描述文件超限报 413；未知插件报 404。"""
        plugins = self.context.plugins
        await plugins.install_archive(
            _plugin_archive("big-readme", "BigDoc", readme="x" * (10 * 1024 * 1024 + 1)),
            replace=True,
        )
        with self.assertRaises(PluginManagerError) as caught:
            await plugins.plugin_readme("big-readme")
        self.assertEqual(caught.exception.status_code, 413)

        status, _body = await _arequest(self.base_url, "/api/plugins/big-readme/readme")
        self.assertEqual(status, 413)
        status, _body = await _arequest(self.base_url, "/api/plugins/ghost/readme")
        self.assertEqual(status, 404)

    async def test_manifest_rejects_invalid_kind_and_requires(self) -> None:
        status, body = await _arequest(
            self.base_url,
            "/api/plugins/upload",
            method="POST",
            body=_plugin_archive("bad-kind", "BadKind", kind="weird"),
            content_type="application/zip",
        )
        self.assertEqual(status, 400)
        self.assertIn("kind", json.loads(body)["error"])

        status, body = await _arequest(
            self.base_url,
            "/api/plugins/upload",
            method="POST",
            body=_plugin_archive("no-kind", "NoKind", kind=None),
            content_type="application/zip",
        )
        self.assertEqual(status, 400)
        self.assertIn("kind", json.loads(body)["error"])

        status, body = await _arequest(
            self.base_url,
            "/api/plugins/upload",
            method="POST",
            body=_plugin_archive("bad-requires", "BadRequires", requires=["ok", ""]),
            content_type="application/zip",
        )
        self.assertEqual(status, 400)
        self.assertIn("requires", json.loads(body)["error"])

    async def test_builtin_plugins_listed_and_locked(self) -> None:
        """内置插件展示于列表，但禁止安装 / 升级 / 启停 / 卸载 / 配置修改。"""
        plugins = self.context.plugins
        listing = {item["id"]: item for item in await plugins.list_plugins()}
        for plugin_id in ("appconfig", "db", "webapi", "plugin-manager"):
            record = listing[plugin_id]
            self.assertTrue(record["builtin"])
            self.assertTrue(record["protected"])
            self.assertTrue(record["enabled"])  # 恒为运行中

        # 影子安装：同 id 上传一律拒绝（否则与直装服务同名冲突）
        with self.assertRaises(PluginManagerError) as caught:
            await plugins.install_archive(
                _plugin_archive("webapi", "ShadowWebApi"), replace=True
            )
        self.assertEqual(caught.exception.status_code, 403)
        status, _body = await _arequest(
            self.base_url,
            "/api/plugins/upload?replace=1",
            method="POST",
            body=_plugin_archive("appconfig", "ShadowConfig"),
            content_type="application/zip",
        )
        self.assertEqual(status, 403)

        for action in (plugins.enable, plugins.disable, plugins.uninstall):
            with self.assertRaises(PluginManagerError):
                await action("appconfig")

        # 配置修改同样禁止（内置插件随平台直装，配置由平台管理）
        with self.assertRaises(PluginManagerError) as caught:
            await plugins.update_config("appconfig", {"path": "x"})
        self.assertEqual(caught.exception.status_code, 403)

    async def test_update_config_merges_persists_and_reloads(self) -> None:
        """部分更新：合并进安装记录、热更新运行时，并写回 config.ini 的对应节。"""
        plugins = self.context.plugins
        await plugins.install_archive(_config_plugin_archive())
        await plugins.enable("config-sample")
        config_path = Path(self.temp_dir.name) / "config.ini"

        record = await plugins.update_config("config-sample", {"port": 9001})
        self.assertEqual(record["config"], {"port": 9001})
        text = config_path.read_text(encoding="utf-8")
        self.assertIn("[sample]", text)
        self.assertIn("port = 9001", text)

        # 第二次部分更新是合并（不覆盖整个配置）
        record = await plugins.update_config("config-sample", {"host": "10.0.0.1"})
        self.assertEqual(record["config"], {"port": 9001, "host": "10.0.0.1"})
        self.assertIn("host = 10.0.0.1", config_path.read_text(encoding="utf-8"))

        # HTTP 入口与服务同一条链路
        status, payload = await _arequest(
            self.base_url,
            "/api/plugins/config-sample/config",
            method="PATCH",
            body=json.dumps({"port": 9002}).encode(),
            content_type="application/json",
        )
        self.assertEqual(status, 200)
        updated = json.loads(payload)["plugin"]
        self.assertEqual(updated["config"], {"port": 9002, "host": "10.0.0.1"})

    async def test_update_config_rejects_invalid_and_locked(self) -> None:
        """校验失败 / 空配置 / 未知插件 / 内置插件 / 非法请求体：报错明确且不落盘。"""
        plugins = self.context.plugins
        await plugins.install_archive(_config_plugin_archive())
        await plugins.enable("config-sample")
        config_path = Path(self.temp_dir.name) / "config.ini"

        with self.assertRaises(PluginManagerError) as caught:
            await plugins.update_config("config-sample", {"port": "abc"})
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("port", str(caught.exception))  # 类型问题消息内含字段名
        self.assertNotIn("[sample]", config_path.read_text(encoding="utf-8"))

        with self.assertRaises(PluginManagerError) as caught:
            await plugins.update_config("config-sample", {})
        self.assertEqual(caught.exception.status_code, 400)

        with self.assertRaises(PluginManagerError) as caught:
            await plugins.update_config("nope", {"port": 1})
        self.assertEqual(caught.exception.status_code, 404)

        with self.assertRaises(PluginManagerError) as caught:
            await plugins.update_config("webapi", {"port": 1})
        self.assertEqual(caught.exception.status_code, 403)

        status, _payload = await _arequest(
            self.base_url,
            "/api/plugins/config-sample/config",
            method="PATCH",
            body=b"not json",
            content_type="application/json",
        )
        self.assertEqual(status, 400)

        # 请求体超限：413 拒绝（与其它入口同口径的输入上限）
        status, _payload = await _arequest(
            self.base_url,
            "/api/plugins/config-sample/config",
            method="PATCH",
            body=b"x" * (MAX_CONFIG_BODY_BYTES + 1),
            content_type="application/json",
        )
        self.assertEqual(status, 413)

    async def test_update_config_saves_when_disabled_and_reports_reload_failure(self) -> None:
        """未启用只保存（启用时生效）；重载失败如实报 422 且配置已保存。"""
        plugins = self.context.plugins
        await plugins.install_archive(_config_plugin_archive("config-offline"))
        record = await plugins.update_config("config-offline", {"port": 9100})
        self.assertEqual(record["config"], {"port": 9100})  # 未启用：只存记录
        self.assertFalse(record["enabled"])

        await plugins.install_archive(_config_plugin_archive("config-crash"))
        await plugins.enable("config-crash")
        with self.assertRaises(PluginManagerError) as caught:
            await plugins.update_config("config-crash", {"port": 6666})
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("配置已保存", str(caught.exception))
        listing = {item["id"]: item for item in await plugins.list_plugins()}
        self.assertEqual(listing["config-crash"]["config"], {"port": 6666})
        self.assertIsNotNone(listing["config-crash"]["last_error"])

    async def test_enable_reports_unready_dependencies(self) -> None:
        """依赖的服务未就绪：422 明说缺哪个服务，不再用「状态未激活」掩盖。"""
        plugins = self.context.plugins
        await plugins.install_archive(
            _plugin_archive("needs-ghost", "NeedsGhost", inject=["ghost-service"])
        )
        with self.assertRaises(PluginManagerError) as caught:
            await plugins.enable("needs-ghost")
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("ghost-service", str(caught.exception))
        listing = {item["id"]: item for item in await plugins.list_plugins()}
        self.assertFalse(listing["needs-ghost"]["enabled"])
        self.assertIsNotNone(listing["needs-ghost"]["last_error"])

    async def test_enable_recovers_from_dead_fiber(self) -> None:
        """重载失败留下的死 fiber 不再让 enable 静默 no-op：清理残留并全新装载。"""
        plugins = self.context.plugins
        await plugins.install_archive(_config_plugin_archive("config-dead"))
        await plugins.enable("config-dead")
        with self.assertRaises(PluginManagerError):
            await plugins.update_config("config-dead", {"port": 6666})

        # 重载失败后如实置为未启用（不再假装「运行中」），错误留档
        listing = {item["id"]: item for item in await plugins.list_plugins()}
        self.assertFalse(listing["config-dead"]["enabled"])
        self.assertIsNotNone(listing["config-dead"]["last_error"])

        # 修复配置后 enable 直接恢复（无需「先停再启」的隐式路径）
        await plugins.update_config("config-dead", {"port": 8123})
        record = await plugins.enable("config-dead")
        self.assertTrue(record["enabled"])
        self.assertIsNone(record["last_error"])

    async def test_registry_drops_builtin_shadow_records(self) -> None:
        """遗留注册表里的内置 id 影子记录读取即丢弃（展示真身内置记录）。"""
        install_root = Path(self.temp_dir.name) / "shadow-root"
        install_root.mkdir()
        PluginRecordStore(install_root / "registry.db").write(
            {
                "webapi": {
                    "id": "webapi",
                    "name": "影子",
                    "version": "9.9.9",
                    "entrypoint": "plugin:Shadow",
                    "resource_root": None,
                    "config": {},
                    "requires": [],
                    "kind": ["any"],
                    "enabled": False,
                    "last_error": None,
                    "digest": "deadbeef",
                }
            }
        )
        manager = PluginManager(self.context, install_root)
        listing = {item["id"]: item for item in await manager.list_plugins()}
        self.assertNotIn("webapi", manager.records)  # 影子被丢弃
        self.assertTrue(listing["webapi"]["builtin"])  # 展示的是内置真身
        self.assertEqual(listing["webapi"]["name"], "Web API 服务")

    async def test_protected_webui_lifecycle_resets_on_restart(self) -> None:
        """预装 webui 允许停用 / 卸载；重启（ensure_installed）自动重装重置。"""
        plugins = self.context.plugins
        listing = {item["id"]: item for item in await plugins.list_plugins()}
        self.assertTrue(listing["webui"]["protected"])
        self.assertFalse(listing["webui"]["builtin"])

        # 停用正常（不是锁定操作）
        record = await plugins.disable("webui")
        self.assertFalse(record["enabled"])

        # 卸载正常
        await plugins.uninstall("webui")
        self.assertNotIn(
            "webui", {item["id"] for item in await plugins.list_plugins()}
        )

        # 重启语义：ensure_installed（启动预装链路）自动重装并启用
        self.assertEqual(
            await ensure_installed(self.context, _SOURCE_ROOT / "master" / "webui"),
            "webui",
        )
        listing = {item["id"]: item for item in await plugins.list_plugins()}
        self.assertTrue(listing["webui"]["enabled"])
        self.assertTrue(listing["webui"]["protected"])

    async def test_preinstalled_marker_follows_config_ini(self) -> None:
        """protected 由 config.ini 的 [node] plugins 推导：dir / zip 源均识别，
        坏源只告警跳过，上传的插件不带标记。"""
        zip_path = Path(self.temp_dir.name) / "extra.zip"
        zip_path.write_bytes(_plugin_archive("extra-sample", "ExtraPlugin"))
        config_path = Path(self.temp_dir.name) / "derive.ini"
        config_path.write_text(
            "[node]\n"
            "kind = master\n"
            f"install_root = {Path(self.temp_dir.name) / 'derive-installed'}\n"
            f"plugins = {_SOURCE_ROOT / 'master' / 'webui'},"
            f" {zip_path}, {Path(self.temp_dir.name) / 'missing'}\n"
            "\n[web]\n"
            f"port = {_free_port()}\n"
            "auth_token = test-token\n",
            encoding="utf-8",
        )
        context: Context = Context()
        context.provide("node_kind", "master")
        await context.plugin(AppConfigPlugin, {"path": str(config_path)})
        await context.plugin(WebApiPlugin)
        await context.plugin(PluginManagerPlugin)
        try:
            plugins = context.plugins
            await plugins.install_source(_SOURCE_ROOT / "master" / "webui")
            await plugins.install_archive(zip_path.read_bytes())
            await plugins.install_archive(_plugin_archive("plain-sample", "PlainPlugin"))
            listing = {item["id"]: item for item in await plugins.list_plugins()}
            self.assertTrue(listing["webui"]["protected"])  # config 列出的目录源
            self.assertTrue(listing["extra-sample"]["protected"])  # zip 源同样识别
            self.assertFalse(listing["plain-sample"]["protected"])  # 上传的不算
        finally:
            await unload_all(context)

    async def test_install_source_installs_and_rejects_duplicates(self) -> None:
        service = self.context.plugins
        # setup 已从 master/webui 安装；重复安装同一插件源应被拒（409 + 新旧版本详情）
        with self.assertRaises(PluginManagerError) as caught:
            await service.install_source(_SOURCE_ROOT / "master" / "webui")
        self.assertEqual(caught.exception.status_code, 409)
        details = caught.exception.details or {}
        self.assertEqual(details["code"], "already-installed")
        self.assertEqual(details["incoming_version"], "1.0.0")

        with self.assertRaises(PluginManagerError):
            await service.install_source(_SOURCE_ROOT / "master" / "nope")

        record = next(item for item in await service.list_plugins() if item["id"] == "webui")
        self.assertEqual(record["kind"], ["master"])
        self.assertTrue(await service.package_bytes("webui"))

    async def test_replace_install_requires_confirmation_and_keeps_state(self) -> None:
        """同 id 覆盖安装：未确认报 409（不区分升/降级），确认后替换且保留启用状态。"""
        service = self.context.plugins
        await service.install_archive(_plugin_archive("swap", "SwapPlugin", version="1.0.0"))
        await service.enable("swap")

        # 不区分版本方向：传入“更低”版本同样弹出 409（由界面显示确认）
        with self.assertRaises(PluginManagerError) as caught:
            await service.install_archive(_plugin_archive("swap", "SwapPlugin", version="0.9.0"))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.details,
            {
                "code": "already-installed",
                "id": "swap",
                "installed_version": "1.0.0",
                "incoming_version": "0.9.0",
            },
        )

        # 显式确认（replace=True）后覆盖安装：代码换成新版，启用状态保留
        record = await service.install_archive(
            _plugin_archive("swap", "SwapPlugin", version="2.0.0"), replace=True
        )
        self.assertEqual(record["version"], "2.0.0")
        self.assertTrue(record["enabled"])
        status, body = await _arequest(self.base_url, "/api/swap")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], "2.0.0")

        # 旧版 zip 归档保留一版供回滚
        archives = Path(self.temp_dir.name) / "installed" / ".archives"
        self.assertTrue((archives / "swap.zip").is_file())
        self.assertTrue((archives / "swap.prev-1.0.0.zip").is_file())

    async def test_replace_install_rolls_back_on_broken_new_version(self) -> None:
        """新版启用失败：整体回滚到旧版（代码、注册表、归档），旧版继续运行。"""
        service = self.context.plugins
        await service.install_archive(_plugin_archive("rb", "RbPlugin", version="1.0.0"))
        await service.enable("rb")

        with self.assertRaises(PluginManagerError) as caught:
            await service.install_archive(
                _plugin_archive("rb", "RbPlugin", version="2.0.0", broken=True), replace=True
            )
        self.assertIn("回滚", str(caught.exception))

        record = next(item for item in await service.list_plugins() if item["id"] == "rb")
        self.assertEqual(record["version"], "1.0.0")
        self.assertIn("覆盖安装失败", record["last_error"])

        # 旧版代码仍在运行
        status, body = await _arequest(self.base_url, "/api/rb")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], "1.0.0")

        # 归档回到旧版 zip，回滚备份不残留
        archives = Path(self.temp_dir.name) / "installed" / ".archives"
        self.assertFalse((archives / "rb.prev-1.0.0.zip").exists())
        self.assertTrue((archives / "rb.zip").is_file())

    async def test_replace_install_keeps_persisted_config(self) -> None:
        """覆盖安装保留用户持久化配置：升级不再把配置打回默认值。"""
        service = self.context.plugins
        await service.install_archive(_config_plugin_archive("upgrade-cfg"))
        await service.enable("upgrade-cfg")
        await service.update_config("upgrade-cfg", {"port": 9100})

        # 同 id 换装：配置保留，重新启用后运行态同样生效
        await service.install_archive(_config_plugin_archive("upgrade-cfg"), replace=True)
        listing = {item["id"]: item for item in await service.list_plugins()}
        self.assertEqual(listing["upgrade-cfg"]["config"], {"port": 9100})
        self.assertTrue(listing["upgrade-cfg"]["enabled"])

    async def test_upload_strips_bytecode_cache(self) -> None:
        """上传包中的 __pycache__/旧 .pyc 被剔除，避免升级后命中旧字节码。"""
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("plugin.json", json.dumps(_MANIFEST_BYTECODE))
            package.writestr("plugin.py", "class BytecodePlugin:\n    name = 'bytecode-sample'\n    inject = []\n    def __init__(self, ctx, config):\n        del ctx, config\n")
            package.writestr("__pycache__/plugin.cpython-314.pyc", b"stale")
            package.writestr("helper.pyc", b"stale")

        status, body = await _arequest(
            self.base_url,
            "/api/plugins/upload",
            method="POST",
            body=archive.getvalue(),
            content_type="application/zip",
        )
        self.assertEqual(status, 200, body.decode("utf-8"))
        package_root = Path(self.temp_dir.name) / "installed" / "bytecode-sample"
        self.assertFalse((package_root / "__pycache__").exists())
        self.assertFalse((package_root / "helper.pyc").exists())

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

    async def test_upload_rejects_lying_member_sizes(self) -> None:
        """声明大小不可信：谎小想绕过解压上限、谎大想骗过预留检查，都会被拒。

        （复核结论：Python zipfile 以声明大小截断读取且 CRC 兑底，谎小的篡改包
        会直接判无效；谎大则被声明值累计的 128 MiB 预留检查拦下。）
        """
        plugins = self.context.plugins
        base = _plugin_archive("liar-sample", "LiarPlugin")

        lying_small = _patch_member_declared_size(base, "ui/Home.vue", 4)
        with self.assertRaises(PluginManagerError):
            await plugins.install_archive(lying_small)

        lying_big = _patch_member_declared_size(base, "ui/Home.vue", 200 * 1024 * 1024)
        with self.assertRaises(PluginManagerError) as caught:
            await plugins.install_archive(lying_big)
        self.assertEqual(caught.exception.status_code, 413)

    def test_builtin_specs_rejects_id_collision(self) -> None:
        """内置插件 id 撞键显式报错（不静默覆盖）。"""

        class _First:
            name = "dup-builtin"

        class _Second:
            name = "dup-builtin"

        _First.__module__ = "common.plugins.demo_a"
        _Second.__module__ = "common.plugins.demo_b"
        fake = module_types.SimpleNamespace(
            registry={
                1: module_types.SimpleNamespace(callback=_First),
                2: module_types.SimpleNamespace(callback=_Second),
            }
        )
        with self.assertRaisesRegex(ValueError, "内置插件 id 冲突"):
            builtin_specs(cast(Context, fake))

    async def test_operations_emit_audit_logs(self) -> None:
        """操作审计（LOG-A1）：安装/启用/改配置/停用/卸载成功路径都有「审计」日志。"""
        captured: list[str] = []

        def capture(message: Message) -> None:
            captured.append(Logger.format(capture_exporter, message))

        capture_exporter: Exporter = {"levels": {"default": 3}, "export": capture}
        self.context.logger.exporter(capture_exporter, raw=True)
        plugins = self.context.plugins
        await plugins.install_archive(_plugin_archive())
        await plugins.enable("uploaded-sample")
        await plugins.enable("uploaded-sample")  # 幂等重入：审计不重复留痕
        await plugins.disable("uploaded-sample")
        await plugins.update_config("uploaded-sample", {"note": "审计测试"})
        await plugins.uninstall("uploaded-sample")
        joined = "\n".join(captured)
        for fragment in (
            "审计：安装插件",
            "审计：启用插件",
            "审计：停用插件",
            "审计：更新插件",
            "审计：卸载插件",
        ):
            self.assertIn(fragment, joined)
        self.assertEqual(joined.count("审计：启用插件"), 1)

    async def test_registry_write_failure_rolls_back_install(self) -> None:
        class RegistryWriteFailureManager(PluginManager):
            def _write_registry(self) -> None:
                raise OSError("simulated registry failure")

        install_root = Path(self.temp_dir.name) / "failed-install"
        manager = RegistryWriteFailureManager(self.context, install_root)
        with self.assertRaises(PluginManagerError):
            await manager.install_archive(_plugin_archive())

        self.assertEqual(manager.records, {})
        self.assertFalse((install_root / "uploaded-sample").exists())
        # 持久化事实源（registry.db）构造即建且回滚不删——除它外不应有残留
        self.assertEqual({p.name for p in install_root.iterdir()}, {"registry.db"})
    async def test_enable_registry_failure_disposes_plugin_fiber(self) -> None:
        class FailEnabledWriteManager(PluginManager):
            def __init__(self, ctx: Context, root: Path) -> None:
                super().__init__(ctx, root)
                self.failed: bool = False

            def _write_registry(self) -> None:
                record = self.records.get(plugin_id)
                if record is not None and record["enabled"] and not self.failed:
                    self.failed = True
                    raise OSError("simulated registry failure")
                super()._write_registry()

        install_root = Path(self.temp_dir.name) / "enable-failure"
        plugin_id = "enable-failure"
        manager = FailEnabledWriteManager(self.context, install_root)
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
        manager = ConcurrentPluginManager(self.context, install_root)
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
        manager = BlockingPluginManager(self.context, install_root)
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


class CrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """PS-H1~H4：安装 / 卸载 / 换装中断（kill 模拟）后的重启自愈与明确诊断。"""

    async def asyncSetUp(self) -> None:
        self.context: Context = Context()
        self.temp_dir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.install_root: Path = Path(self.temp_dir.name) / "installed"
        self.context.provide("node_kind", "master")

    async def asyncTearDown(self) -> None:
        await unload_all(self.context)
        self.temp_dir.cleanup()

    async def test_missing_dir_gives_reinstall_diagnosis(self) -> None:
        """PS-H1：registry 有记录、目录缺失 → 明确诊断，不是裸 ModuleNotFoundError。"""
        manager = PluginManager(self.context, self.install_root)
        await manager.install_archive(_plugin_archive())
        # kill 模拟：registry 写成功后安装目录丢失（卸载中断 / 磁盘清理）
        shutil.rmtree(self.install_root / "uploaded-sample")

        restarted = PluginManager(self.context, self.install_root)
        self.assertIn("需要重新安装", restarted.records["uploaded-sample"]["last_error"] or "")
        with self.assertRaises(PluginManagerError) as raised:
            await restarted.enable("uploaded-sample")
        self.assertIn("需要重新安装", str(raised.exception))
        public = next(
            record
            for record in await restarted.list_plugins()
            if record["id"] == "uploaded-sample"
        )
        self.assertIn("需要重新安装", public["last_error"] or "")

    async def test_orphan_dir_is_overwritten_not_conflicting(self) -> None:
        """PS-H2：孤儿安装目录（registry 无记录）→ 不再 409 卡死，安装时覆盖。"""
        manager = PluginManager(self.context, self.install_root)
        await manager.install_archive(_plugin_archive())
        # kill 模拟：目录落地但 registry 未写（整库丢失）
        (self.install_root / "registry.db").unlink()
        # 另一个半截落地的目录（安装到一半被 kill）
        orphan = self.install_root / "half-installed"
        orphan.mkdir()
        (orphan / "garbage.txt").write_text("partial", encoding="utf-8")

        restarted = PluginManager(self.context, self.install_root)
        self.assertEqual(restarted.records, {})
        record = await restarted.install_archive(_plugin_archive())
        self.assertEqual(record["id"], "uploaded-sample")
        self.assertTrue((self.install_root / "uploaded-sample" / "plugin.json").is_file())
        # 半截孤儿同样被同名安装覆盖（不 409）
        await restarted.install_archive(_plugin_archive(plugin_id="half-installed"))
        self.assertFalse((self.install_root / "half-installed" / "garbage.txt").exists())

    async def test_incompatible_schema_version_is_explicit(self) -> None:
        """PS-H3：registry schema 版本不兼容 → 启动显式报错（不再静默半兼容）。"""
        manager = PluginManager(self.context, self.install_root)
        await manager.install_archive(_plugin_archive())
        conn = sqlite3.connect(self.install_root / "registry.db")
        try:
            with conn:
                conn.execute(
                    "UPDATE store_meta SET value = '999' WHERE name = 'schema_version'"
                )
        finally:
            conn.close()
        with self.assertRaises(RuntimeError) as raised:
            PluginManager(self.context, self.install_root)
        self.assertIn("schema 版本不兼容", str(raised.exception))

    async def test_interrupted_replace_restarts_cleanly(self) -> None:
        """PS-H4：换装中断（目录已清、registry 停旧）→ 重启可重装，诊断自动清除。"""
        manager = PluginManager(self.context, self.install_root)
        await manager.install_archive(_plugin_archive())
        await manager.install_archive(_plugin_archive(version="2.0.0"), replace=True)
        # kill 模拟：换装清旧阶段中断
        shutil.rmtree(self.install_root / "uploaded-sample")

        restarted = PluginManager(self.context, self.install_root)
        self.assertIn("uploaded-sample", restarted.records)
        record = await restarted.install_archive(
            _plugin_archive(version="2.0.0"), replace=True
        )
        self.assertEqual(record["version"], "2.0.0")
        self.assertIsNone(record["last_error"])


    async def test_preinstalled_dirs_are_not_flagged_as_orphans(self) -> None:
        """预装插件目录由 ensure_installed 管理，不判孤儿；真孤儿照常告警。"""
        (self.install_root / "webui").mkdir(parents=True)
        (self.install_root / "stray").mkdir(parents=True)
        captured: list[str] = []
        self.context.logger.exporter(
            {"levels": {"default": 1}, "export": lambda message: captured.append(str(message.args))},
            raw=True,
        )
        PluginManager(self.context, self.install_root, ["webui"])
        messages = "\n".join(captured)
        self.assertIn("stray", messages)
        self.assertNotIn("webui", messages)


if __name__ == "__main__":
    unittest.main()
