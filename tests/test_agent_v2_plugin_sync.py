"""Agent V2 插件安装和同步测试。"""

from __future__ import annotations

import hashlib

import pytest
from aetp_protocol.ids import BusinessId, PluginId, SemVer, SessionId, Sha256
from aetp_protocol.plugin_types import PluginDistributionRef, PluginSyncAction
from aetp_protocol.plugins import PluginSyncItem, PluginSyncRequest

from agent.application.services.plugin_sync_service import AgentPluginSyncService
from agent.plugins.errors import PluginInstallError
from agent.plugins.v2_installer import V2PluginInstaller
from agent.plugins.v2_registry import AgentV2PluginRegistry
from tests.test_v2_plugin_archive import _archive

NODE_ID = BusinessId("01J00000000000000000000000")
PLUGIN_ID = PluginId("org.example.executor")
VERSION = SemVer("2.0.0")
SESSION_ID = SessionId("session-00000001")
SYNC_ID = BusinessId("01J00000000000000000000001")


def _package(content: bytes, *, url: str = "https://master/plugin.zip") -> PluginDistributionRef:
    return PluginDistributionRef(
        plugin_id=PLUGIN_ID,
        version=VERSION,
        archive_sha256=Sha256(hashlib.sha256(content).hexdigest()),
        download_url=url,
    )


def test_v2_installer_is_immutable_and_does_not_load_code(tmp_path) -> None:
    content = _archive()
    package = _package(content)
    installer = V2PluginInstaller(tmp_path, fetcher=lambda _: content)

    installed = installer.install(package)
    repeated = installer.install(package)

    assert installed.ref == repeated.ref
    assert installed.manifest_path.is_file()
    assert (installed.install_path / "plugin-ref.json").is_file()
    with pytest.raises(PluginInstallError):
        installer.install(_package(content + b"changed"))

    installer.remove(PLUGIN_ID, VERSION)
    assert not installed.install_path.exists()


def test_agent_v2_sync_checks_session_and_returns_typed_result(tmp_path) -> None:
    content = _archive()
    package = _package(content)
    request = PluginSyncRequest(
        sync_id=SYNC_ID,
        node_id=NODE_ID,
        expected_session_id=SESSION_ID,
        items=(
            PluginSyncItem(
                plugin_id=PLUGIN_ID,
                version=VERSION,
                action=PluginSyncAction.INSTALL,
                package=package,
            ),
        ),
    )
    registry = AgentV2PluginRegistry()
    service = AgentPluginSyncService(
        V2PluginInstaller(tmp_path, fetcher=lambda _: content),
        SESSION_ID,
        registry,
    )

    result = service.apply(request)
    stale = service.apply(request.model_copy(update={"expected_session_id": SessionId("session-00000002")}))

    assert result.accepted is True
    assert result.restart_required is True
    assert result.items[0].state == "installed"
    assert registry.get(PLUGIN_ID.root, VERSION.root) is not None
    assert stale.accepted is False
    assert stale.items[0].unavailable_reasons[0].root == "STALE_SESSION"
