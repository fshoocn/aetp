"""Master 插件 UI 静态托管（PluginUiHost）测试。"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from aetp_protocol.ids import PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import PluginPoint, PluginStatus
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest

from master.adapters.plugin_ui.host import PluginUiHost
from master.domain.models import PluginVersionRecord
from master.plugins.extension_resolver import ExtensionResolver
from master.plugins.registry import PluginRegistry

PLUGIN_ID = PluginId("org.example.demo-ui")
VERSION = SemVer("1.0.0")


def _ui_plugin_bytes() -> tuple[bytes, PluginManifest]:
    """构造携带 ui/ 入口的 executor 插件归档（任务插件自带 UI 的规范形态）。"""
    manifest = PluginManifest(
        schema_version=2,
        id=PLUGIN_ID,
        version=VERSION,
        api_version=SemVer("2.0.0"),
        point=PluginPoint.EXECUTOR,
        display_name="Demo Executor UI",
        entrypoints=PluginEntrypoints(
            master="entry:create",
            agent="entry:create",
            ui="ui/index.html",
        ),
        ui_protocol_version=2,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", manifest.model_dump_json())
        archive.writestr("master/entry.py", "def create():\n    return object()\n")
        archive.writestr("agent/entry.py", "def create():\n    return object()\n")
        archive.writestr("ui/index.html", "<html><body>demo ui</body></html>")
        archive.writestr("ui/app.js", "console.log('demo')")
    return buffer.getvalue(), manifest


def _install_ui_plugin(tmp_path: Path) -> tuple[PluginRegistry, PluginVersionRecord]:
    archive_path = tmp_path / "archives" / "demo-ui.zip"
    archive_path.parent.mkdir(parents=True)
    content, manifest = _ui_plugin_bytes()
    archive_path.write_bytes(content)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    registry = PluginRegistry(tmp_path / "archives")
    record = PluginVersionRecord(
        id=None,
        plugin_id=manifest.id,
        version=manifest.version,
        point=manifest.point,
        status=PluginStatus.ENABLED,
        filename="demo-ui.zip",
        archive_sha256=Sha256(digest),
        manifest_sha256=Sha256("a" * 64),
        manifest=manifest,
        archive_path=str(archive_path),
        installed_at=None,
        created_at=None,
        updated_at=None,
    )
    registry.register(record)
    return registry, record


def _host(tmp_path: Path) -> tuple[PluginUiHost, PluginRegistry]:
    registry, _record = _install_ui_plugin(tmp_path)
    resolver = ExtensionResolver(registry, tmp_path / "runtime")
    return PluginUiHost(registry, resolver), registry


def test_host_serves_default_entry_and_sub_assets(tmp_path: Path) -> None:
    host, _registry = _host(tmp_path)

    default = host.resolve(PLUGIN_ID.root, VERSION.root, None)

    assert default is not None
    assert default.path.name == "index.html"
    assert default.media_type.startswith("text/html")
    assert default.path.is_file()

    asset = host.resolve(PLUGIN_ID.root, VERSION.root, "app.js")
    assert asset is not None and asset.path.name == "app.js"
    assert asset.media_type == "text/javascript; charset=utf-8"


def test_host_rejects_traversal_and_missing_files(tmp_path: Path) -> None:
    host, _registry = _host(tmp_path)

    assert host.resolve(PLUGIN_ID.root, VERSION.root, "..%2F..%2Fsecret") is None
    assert host.resolve(PLUGIN_ID.root, VERSION.root, "../../master/main.py") is None
    assert host.resolve(PLUGIN_ID.root, VERSION.root, "missing.js") is None


def test_host_returns_none_for_unknown_plugin_or_version(tmp_path: Path) -> None:
    host, _registry = _host(tmp_path)

    assert host.resolve("org.example.missing", VERSION.root, None) is None
    assert host.resolve(PLUGIN_ID.root, "9.9.9", None) is None
    assert host.resolve("not-an-id!", VERSION.root, None) is None


def _executor_with_ui_record(tmp_path: Path) -> tuple[PluginRegistry, PluginVersionRecord]:
    """构造携带 ui/ 入口的 executor 插件并注册为 ENABLED。"""
    archive_path = tmp_path / "archives" / "exec-with-ui.zip"
    archive_path.parent.mkdir(parents=True)
    manifest = PluginManifest(
        schema_version=2,
        id=PluginId("org.example.exec-ui"),
        version=SemVer("1.0.0"),
        api_version=SemVer("2.0.0"),
        point=PluginPoint.EXECUTOR,
        display_name="Exec UI",
        entrypoints=PluginEntrypoints(
            master="entry:create",
            agent="entry:create",
            ui="ui/index.html",
        ),
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", manifest.model_dump_json())
        archive.writestr("master/entry.py", "def create():\n    return object()\n")
        archive.writestr("agent/entry.py", "def create():\n    return object()\n")
        archive.writestr("ui/index.html", "<html><body>exec ui</body></html>")
        archive.writestr("ui/config.js", "console.log('cfg')")
    archive_path.write_bytes(buffer.getvalue())
    registry = PluginRegistry(tmp_path / "archives")
    record = PluginVersionRecord(
        id=None,
        plugin_id=manifest.id,
        version=manifest.version,
        point=manifest.point,
        status=PluginStatus.ENABLED,
        filename="exec-with-ui.zip",
        archive_sha256=Sha256(hashlib.sha256(archive_path.read_bytes()).hexdigest()),
        manifest_sha256=Sha256("c" * 64),
        manifest=manifest,
        archive_path=str(archive_path),
        installed_at=None,
        created_at=None,
        updated_at=None,
    )
    registry.register(record)
    return registry, record


def test_host_ignores_plugin_without_ui_entry(tmp_path: Path) -> None:
    """无 ui 入口的 executor 不被 UI 托管（原 test_host_ignores_non_ui_point）。"""
    archive_path = tmp_path / "archives" / "exec.zip"
    archive_path.parent.mkdir(parents=True)
    manifest = PluginManifest(
        schema_version=2,
        id=PluginId("org.example.exec"),
        version=SemVer("1.0.0"),
        api_version=SemVer("2.0.0"),
        point=PluginPoint.EXECUTOR,
        display_name="Exec",
        entrypoints=PluginEntrypoints(master="entry:create", agent="entry:create"),
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", manifest.model_dump_json())
        archive.writestr(
            "master/entry.py",
            "def create():\n    return object()\n",
        )
        archive.writestr(
            "agent/entry.py",
            "def create():\n    return object()\n",
        )
    archive_path.write_bytes(buffer.getvalue())
    registry = PluginRegistry(tmp_path / "archives")
    record = PluginVersionRecord(
        id=None,
        plugin_id=manifest.id,
        version=manifest.version,
        point=manifest.point,
        status=PluginStatus.ENABLED,
        filename="exec.zip",
        archive_sha256=Sha256(hashlib.sha256(archive_path.read_bytes()).hexdigest()),
        manifest_sha256=Sha256("b" * 64),
        manifest=manifest,
        archive_path=str(archive_path),
        installed_at=None,
        created_at=None,
        updated_at=None,
    )
    registry.register(record)

    host = PluginUiHost(registry, ExtensionResolver(registry, tmp_path / "runtime"))

    assert host.resolve("org.example.exec", "1.0.0", None) is None


def test_host_serves_executor_plugin_with_ui_entry(tmp_path: Path) -> None:
    """executor 等非 ui point 的插件只要声明 ui 入口，就能被 UI 托管。"""
    registry, _record = _executor_with_ui_record(tmp_path)
    host = PluginUiHost(registry, ExtensionResolver(registry, tmp_path / "runtime"))

    default = host.resolve("org.example.exec-ui", "1.0.0", None)

    assert default is not None
    assert default.path.name == "index.html"
    assert "exec ui" in default.path.read_text(encoding="utf-8")

    asset = host.resolve("org.example.exec-ui", "1.0.0", "config.js")
    assert asset is not None and asset.path.name == "config.js"


def test_http_serves_enabled_plugin_ui_via_governance_flow(client) -> None:
    """经完整治理流程启用 UI 插件后，HTTP 端点可返回默认文档与子资源。"""
    content, _manifest = _ui_plugin_bytes()
    container = client.app.state.container
    governance = container.plugin_governance_service()
    record = governance.register_archive("demo-ui.zip", content)
    record = governance.install(record.plugin_id, record.version)
    governance.request_enabled(record.plugin_id, record.version)
    record = governance.complete_restart(record.plugin_id, record.version, enabled=True)
    container.plugin_registry().register(record)

    root = client.get("/plugins/org.example.demo-ui/1.0.0/ui")
    assert root.status_code == 200, root.text
    assert "demo ui" in root.text

    asset = client.get("/plugins/org.example.demo-ui/1.0.0/ui/app.js")
    assert asset.status_code == 200, asset.text
    assert "demo" in asset.text

    missing = client.get("/plugins/org.example.demo-ui/1.0.0/ui/nope.js")
    assert missing.status_code == 404
    traversal = client.get("/plugins/org.example.demo-ui/1.0.0/ui/../../plugin.json")
    assert traversal.status_code == 404
    unknown = client.get("/plugins/org.example.missing/1.0.0/ui")
    assert unknown.status_code == 404
