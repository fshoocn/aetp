"""Master 插件热插拔（PluginHotReloader）测试：启停无需重启即反映到装配面。"""

from __future__ import annotations

import io
import zipfile

from aetp_protocol.ids import PluginId, SemVer
from aetp_protocol.plugin_types import PluginPoint, PluginStatus
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest

PLUGIN_ID = PluginId("org.example.hot.reporter")
VERSION = SemVer("1.0.0")


def _reporter_archive() -> bytes:
    """Master-only REPORTER 插件归档：master/entry.py 返回带 report() 与插件标识的对象。"""
    manifest = PluginManifest(
        schema_version=2,
        id=PLUGIN_ID,
        version=VERSION,
        api_version=SemVer("2.0.0"),
        point=PluginPoint.REPORTER,
        display_name="Hot Reporter",
        entrypoints=PluginEntrypoints(master="entry:create_reporter"),
    )
    entry = (
        "class R:\n"
        "    plugin_id = 'org.example.hot.reporter'\n"
        "    plugin_version = '1.0.0'\n"
        "    async def report(self, request, context):\n"
        "        return None\n"
        "\n"
        "def create_reporter():\n"
        "    return R()\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", manifest.model_dump_json())
        archive.writestr("master/entry.py", entry)
    return buffer.getvalue()


def test_hot_enable_makes_reporter_available_without_restart(client, tmp_path) -> None:
    """enable（master 面直接 ENABLED）+ refresh 后，registry/reporter 注册表立即可用。"""
    container = client.app.state.container
    governance = container.plugin_governance_service()
    record = governance.register_archive("hot-reporter.zip", _reporter_archive())
    assert record.status is PluginStatus.VERIFIED

    # 安装 + 启用（master 面 → 直接 ENABLED，不再 PENDING_RESTART）
    governance.install(record.plugin_id, record.version)
    enabled = governance.enable(record.plugin_id, record.version)
    assert enabled.status is PluginStatus.ENABLED

    # 模拟 API 层在状态变更后触发的热重载
    container.plugin_hot_reload().refresh()

    registry = container.plugin_registry()
    assert registry.get(PLUGIN_ID, VERSION, PluginPoint.REPORTER) is not None
    reporters = container.reporter_registry()
    assert any(item.plugin_id == PLUGIN_ID.root for item in reporters.list())
    assert any(item.plugin_version == VERSION.root for item in reporters.list())


def test_hot_disable_removes_reporter_without_restart(client, tmp_path) -> None:
    """disable（master 面直接 DISABLED）+ refresh 后，registry/reporter 注册表立即移除。"""
    container = client.app.state.container
    governance = container.plugin_governance_service()
    record = governance.register_archive("hot-reporter.zip", _reporter_archive())
    governance.install(record.plugin_id, record.version)
    governance.enable(record.plugin_id, record.version)
    container.plugin_hot_reload().refresh()
    assert container.reporter_registry().list()  # 已含该 reporter

    disabled = governance.disable(record.plugin_id, record.version)
    assert disabled.status is PluginStatus.DISABLED
    container.plugin_hot_reload().refresh()

    registry = container.plugin_registry()
    assert registry.get(PLUGIN_ID, VERSION, PluginPoint.REPORTER) is None
    assert not any(item.plugin_id == PLUGIN_ID.root for item in container.reporter_registry().list())


def test_hot_remove_drops_plugin_after_disable(client, tmp_path) -> None:
    """移除前置：先 disable 到 DISABLED 再 remove；refresh 后 registry 不再含该版本。"""
    container = client.app.state.container
    governance = container.plugin_governance_service()
    record = governance.register_archive("hot-reporter.zip", _reporter_archive())
    governance.install(record.plugin_id, record.version)
    governance.enable(record.plugin_id, record.version)
    container.plugin_hot_reload().refresh()
    assert container.plugin_registry().get(PLUGIN_ID, VERSION, PluginPoint.REPORTER) is not None

    governance.disable(record.plugin_id, record.version)
    governance.remove(record.plugin_id, record.version)
    container.plugin_hot_reload().refresh()

    assert container.plugin_registry().get(PLUGIN_ID, VERSION, PluginPoint.REPORTER) is None
