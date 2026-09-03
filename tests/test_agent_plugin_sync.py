"""Agent  插件安装和同步测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import pytest
from aetp_protocol.capabilities import NodeCapabilities, NodeCapabilitySnapshot
from aetp_protocol.envelope import Envelope, Sender, parse_message
from aetp_protocol.ids import BusinessId, MessageId, PluginId, SemVer, SessionId, Sha256, TraceId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import MaintenanceStatus
from aetp_protocol.plugin_types import PluginDistributionRef, PluginSyncAction
from aetp_protocol.plugins import PluginSyncItem, PluginSyncRequest, PluginSyncResult
from aetp_protocol.topics import command_topic, event_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.application.services.plugin_sync_controller import PluginSyncController
from agent.application.services.plugin_sync_service import AgentPluginSyncService
from agent.config import AgentSettings
from agent.plugins.errors import PluginInstallError
from agent.plugins.installer import PluginInstaller
from agent.plugins.registry import PluginRegistry
from common.transport import MqttMessage
from tests.test_plugin_archive import _archive

NODE_ID = BusinessId("01J00000000000000000000000")
PLUGIN_ID = PluginId("org.example.executor")
VERSION = SemVer("2.0.0")
SESSION_ID = SessionId("session-00000001")
SYNC_ID = BusinessId("01J00000000000000000000001")


class SyncTransport:
    connected = True

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int]] = []

    def on_message(self, handler) -> None:
        del handler

    def on_connection_change(self, handler) -> None:
        del handler

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def subscribe(self, topics: list[str]) -> None:
        del topics

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.published.append((topic, payload, qos))


def _package(content: bytes, *, url: str = "https://master/plugin.zip") -> PluginDistributionRef:
    return PluginDistributionRef(
        plugin_id=PLUGIN_ID,
        version=VERSION,
        archive_sha256=Sha256(hashlib.sha256(content).hexdigest()),
        download_url=url,
    )


def test_installer_is_immutable_and_does_not_load_code(tmp_path) -> None:
    content = _archive()
    package = _package(content)
    installer = PluginInstaller(tmp_path, fetcher=lambda _: content)

    installed = installer.install(package)
    repeated = installer.install(package)

    assert installed.ref == repeated.ref
    assert installed.manifest_path.is_file()
    assert (installed.install_path / "plugin-ref.json").is_file()
    with pytest.raises(PluginInstallError):
        installer.install(_package(content + b"changed"))

    installer.remove(PLUGIN_ID, VERSION)
    assert not installed.install_path.exists()


def test_registry_restores_metadata_without_loading_entrypoint(tmp_path) -> None:
    content = _archive()
    package = _package(content)
    installer = PluginInstaller(tmp_path, fetcher=lambda _: content)
    installed = installer.install(package)

    restored = PluginRegistry(tmp_path)

    record = restored.get(PLUGIN_ID.root, VERSION.root)
    assert record is not None
    assert record.manifest_path == installed.manifest_path
    assert restored.list() == (record,)


def test_agent_sync_checks_session_and_returns_typed_result(tmp_path) -> None:
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
    registry = PluginRegistry()
    service = AgentPluginSyncService(
        PluginInstaller(tmp_path, fetcher=lambda _: content),
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


def test_sync_controller_completes_command_result_and_snapshot_loop(tmp_path) -> None:
    content = _archive()
    transport = SyncTransport()
    restart_called: list[bool] = []
    settings = AgentSettings(
        node_id=NODE_ID.root,
        name="Bench 01",
        mqtt_client_id="aetp-agent-bench-01",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = PluginRegistry(settings.plugin_dir)
    publisher = CapabilityPublisher(
        transport=transport,
        settings=settings,
        registry=registry,
        capability_scanner=lambda: NodeCapabilities(),
    )
    controller = PluginSyncController(
        NODE_ID,
        ledger,
        PluginInstaller(settings.plugin_dir, fetcher=lambda _: content),
        registry,
        publisher,
        restart=lambda: restart_called.append(True),
    )
    request = PluginSyncRequest(
        sync_id=SYNC_ID,
        node_id=NODE_ID,
        expected_session_id=SESSION_ID,
        items=(
            PluginSyncItem(
                plugin_id=PLUGIN_ID,
                version=VERSION,
                action=PluginSyncAction.INSTALL,
                package=_package(content),
            ),
        ),
    )
    command = Envelope(
        message_id=MessageId("sync-message-0001"),
        sent_at=datetime.now(UTC),
        sender=Sender(
            kind="master",
            id=stable_id("aetp-master"),
            session_id=SessionId("master-session-01"),
        ),
        message_type=MessageType.AGENT_PLUGIN_SYNC.value,
        trace_id=TraceId("sync-trace-0000001"),
        payload=request.model_dump(mode="json"),
    )
    message = MqttMessage(
        topic=command_topic(NODE_ID.root, "agent.plugin.sync"),
        payload=command.model_dump_json().encode("utf-8"),
    )

    assert asyncio.run(controller.handle(message, SESSION_ID)) is True
    assert registry.get(PLUGIN_ID.root, VERSION.root) is not None
    assert [topic for topic, _, _ in transport.published] == [
        event_topic(NODE_ID.root, "agent.maintenance.status"),
        event_topic(NODE_ID.root, "agent.maintenance.status"),
        event_topic(NODE_ID.root, "agent.plugin.sync.result"),
        event_topic(NODE_ID.root, "agent.maintenance.status"),
        event_topic(NODE_ID.root, "capability.snapshot"),
    ]
    parsed = [parse_message(json.loads(payload)) for _, payload, _ in transport.published]
    assert isinstance(parsed[0][1], MaintenanceStatus)
    assert parsed[0][1].state.value == "draining"
    assert parsed[1][1].state.value == "updating"
    assert parsed[2][0].correlation_id == command.message_id
    assert parsed[2][1].accepted is True
    assert isinstance(parsed[4][1], NodeCapabilitySnapshot)
    assert parsed[4][1].plugin_inventory[0].availability.value == "available"
    assert restart_called == [True]

    assert asyncio.run(controller.handle(message, SESSION_ID)) is True
    assert len(transport.published) == 6
    _, repeated_payload, _ = transport.published[-1]
    _, repeated_result = parse_message(json.loads(repeated_payload))
    assert isinstance(repeated_result, PluginSyncResult)
    assert repeated_result.sync_id == SYNC_ID
