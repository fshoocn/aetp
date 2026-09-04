"""Master 节点插件对账与卸载：期望版本 vs Agent 库存，治理移除自动清理。"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime

from aetp_protocol.capabilities import (
    AgentMaintenanceState,
    NodeCapabilitySnapshot,
    PluginInventoryItem,
)
from aetp_protocol.envelope import parse_message
from aetp_protocol.ids import PluginId, SemVer, Sha256, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.plugin_types import (
    DesiredPluginVersion,
    PluginAvailability,
    PluginPoint,
    PluginStatus,
    PluginSyncAction,
)
from aetp_protocol.plugins import PluginSyncItem

from master.domain.models import NodeCapabilitySnapshotRecord
from master.domain.models.plugin_governance import PluginSyncOperationState
from tests.test_plugin_sync_messages import NODE_ID, SESSION_ID, _admin_headers, _seed_node

PLUGIN_ID = PluginId("org.example.executor")
VERSION = SemVer("2.0.0")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _archive_for(plugin_id: str, version: str, point: str) -> bytes:
    """构造最小合法插件归档（executor 带 master+agent 入口，resource 仅 agent 入口）。"""
    entrypoints: dict[str, str] = {}
    files: dict[str, bytes] = {}
    if point in {"executor", "storage", "transport"}:
        entrypoints["master"] = "plugin:create_plugin"
        entrypoints["agent"] = "plugin:create_plugin"
        files["master/plugin.py"] = b"def create_plugin(): pass"
        files["agent/plugin.py"] = b"def create_plugin(): pass"
    else:
        entrypoints["agent"] = "plugin:create_plugin"
        files["agent/plugin.py"] = b"def create_plugin(): pass"
    files["plugin.json"] = json.dumps(
        {
            "schema_version": 2,
            "id": plugin_id,
            "version": version,
            "api_version": "2.0.0",
            "point": point,
            "display_name": f"Plugin {plugin_id}",
            "entrypoints": entrypoints,
        }
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _inventory_item(plugin_id: str, version: str, point: PluginPoint, sha: str = "a" * 64) -> PluginInventoryItem:
    return PluginInventoryItem(
        plugin_id=PluginId(plugin_id),
        point=point,
        version=SemVer(version),
        archive_sha256=Sha256(sha),
        availability=PluginAvailability.AVAILABLE,
        checked_at=NOW,
    )


def _seed_snapshot(container, *items: PluginInventoryItem) -> None:
    snapshot = NodeCapabilitySnapshot(
        schema_version=2,
        node_id=NODE_ID,
        session_id=SESSION_ID,
        revision=1,
        reported_at=NOW,
        maintenance_state=AgentMaintenanceState.IDLE,
        plugin_inventory=items,
    )
    with container.uow_factory()() as uow:
        uow.node_capability_snapshots.add_if_newer(
            NodeCapabilitySnapshotRecord(
                id=None,
                node_id=NODE_ID,
                session_id=SESSION_ID,
                revision=1,
                snapshot_sha256=Sha256("b" * 64),
                snapshot=snapshot,
                reported_at=NOW,
                created_at=NOW,
            )
        )


def _sync_command(container, index: int = 0) -> tuple:
    with container.uow_factory()() as uow:
        operations = uow.agent_plugin_sync_operations.list_by_node(NODE_ID)
        assert operations, "没有插件同步操作记录"
        record = operations[index]
        outbox = uow.outbox_messages.get_by_outbox_id(
            stable_id(f"plugin-sync:{record.sync_id.root}").root
        )
        assert outbox is not None
        envelope, payload = parse_message(outbox.payload)
        assert envelope.message_type == MessageType.AGENT_PLUGIN_SYNC.value
        assert payload.node_id == NODE_ID
        return record, payload


def test_set_desired_plugin_auto_reconciles_install(client) -> None:
    """PUT desired-plugin 后自动对账：为缺失的期望版本下发 INSTALL。"""
    container = client.app.state.container
    _seed_node(container)
    archive = container.plugin_governance_service().register_archive(
        "example.zip", _archive_for(PLUGIN_ID.root, VERSION.root, "executor")
    )
    headers = _admin_headers(client)

    response = client.put(
        f"/api/v2/nodes/{NODE_ID.root}/desired-plugin",
        json={"plugin_id": PLUGIN_ID.root, "point": "executor", "version": VERSION.root},
        headers=headers,
    )
    assert response.status_code == 200

    record, payload = _sync_command(container)
    assert record.state is PluginSyncOperationState.DRAINING
    assert len(payload.items) == 1
    item = payload.items[0]
    assert isinstance(item, PluginSyncItem)
    assert item.action is PluginSyncAction.INSTALL
    assert item.plugin_id == PLUGIN_ID
    assert item.version == VERSION
    assert item.point is PluginPoint.EXECUTOR
    assert item.package is not None
    assert item.package.archive_sha256 == archive.archive_sha256
    assert item.package.download_url is not None


def test_reconcile_dispatches_remove_for_undesired_inventory(client) -> None:
    """Agent 库存里存在无期望的插件 → 对账下发 REMOVE。"""
    container = client.app.state.container
    _seed_node(container)
    _seed_snapshot(container, _inventory_item("org.other.tool", "1.0.0", PluginPoint.RESOURCE))
    headers = _admin_headers(client)

    response = client.post(
        f"/api/v2/nodes/{NODE_ID.root}/plugin-sync/reconcile",
        headers=headers,
    )
    assert response.status_code == 202
    body = response.json()
    assert body is not None
    assert body["state"] == PluginSyncOperationState.DRAINING.value
    assert body["items"][0]["action"] == PluginSyncAction.REMOVE.value
    assert body["items"][0]["plugin_id"] == "org.other.tool"
    assert body["items"][0]["version"] == "1.0.0"
    assert body["items"][0]["package"] is None

    _, payload = _sync_command(container)
    assert payload.items[0].action is PluginSyncAction.REMOVE


def test_reconcile_noop_returns_null_body(client) -> None:
    """无库存且无期望 → 对账为空操作，返回空体。"""
    container = client.app.state.container
    _seed_node(container)

    response = client.post(
        f"/api/v2/nodes/{NODE_ID.root}/plugin-sync/reconcile",
        headers=_admin_headers(client),
    )
    assert response.status_code == 202
    assert response.text in ("", "null")

    with container.uow_factory()() as uow:
        assert uow.agent_plugin_sync_operations.list_by_node(NODE_ID) == []


def test_uninstall_endpoint_removes_plugin_and_clears_desired(client) -> None:
    """DELETE 节点插件版本：下发 REMOVE 并清除期望（防对账复装）。"""
    container = client.app.state.container
    _seed_node(container)
    archive = container.plugin_governance_service().register_archive(
        "example.zip", _archive_for(PLUGIN_ID.root, VERSION.root, "executor")
    )
    _seed_snapshot(
        container,
        _inventory_item(PLUGIN_ID.root, VERSION.root, PluginPoint.EXECUTOR, archive.archive_sha256.root),
    )
    container.plugin_sync_service().set_desired_version(
        NODE_ID,
        DesiredPluginVersion(
            plugin_id=PLUGIN_ID,
            point=PluginPoint.EXECUTOR,
            version=VERSION,
        ),
    )
    headers = _admin_headers(client)

    response = client.delete(
        f"/api/v2/nodes/{NODE_ID.root}/plugins/{PLUGIN_ID.root}/{VERSION.root}",
        headers={**headers, "Idempotency-Key": "uninstall-node-1"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["items"][0]["action"] == PluginSyncAction.REMOVE.value

    with container.uow_factory()() as uow:
        assert uow.agent_plugin_desired_versions.get(NODE_ID, PLUGIN_ID) is None

    # 未安装的插件 → 404
    missing = client.delete(
        f"/api/v2/nodes/{NODE_ID.root}/plugins/org.missing.tool/1.0.0",
        headers={**headers, "Idempotency-Key": "uninstall-node-2"},
    )
    assert missing.status_code == 404

    # 同幂等键重放返回同一结果
    replay = client.delete(
        f"/api/v2/nodes/{NODE_ID.root}/plugins/{PLUGIN_ID.root}/{VERSION.root}",
        headers={**headers, "Idempotency-Key": "uninstall-node-1"},
    )
    assert replay.status_code == 202
    assert replay.json()["sync_id"] == body["sync_id"]


def test_governance_remove_uninstalls_from_nodes(client) -> None:
    """治理移除插件版本后：装有它的在线节点收到 REMOVE，期望被清除。"""
    container = client.app.state.container
    _seed_node(container)
    governance = container.plugin_governance_service()
    archive = governance.register_archive("example.zip", _archive_for(PLUGIN_ID.root, VERSION.root, "executor"))
    governance.install(archive.plugin_id, archive.version)
    governance.disable(archive.plugin_id, archive.version)
    _seed_snapshot(
        container,
        _inventory_item(PLUGIN_ID.root, VERSION.root, PluginPoint.EXECUTOR, archive.archive_sha256.root),
    )
    container.plugin_sync_service().set_desired_version(
        NODE_ID,
        DesiredPluginVersion(
            plugin_id=PLUGIN_ID,
            point=PluginPoint.EXECUTOR,
            version=VERSION,
        ),
    )
    headers = _admin_headers(client)

    response = client.delete(
        f"/api/v2/plugins/{PLUGIN_ID.root}/{VERSION.root}",
        headers={**headers, "Idempotency-Key": "governance-remove-1"},
    )
    assert response.status_code == 204

    record, payload = _sync_command(container)
    assert record.state is PluginSyncOperationState.DRAINING
    assert payload.items[0].action is PluginSyncAction.REMOVE
    assert payload.items[0].version == VERSION

    with container.uow_factory()() as uow:
        assert uow.plugin_versions.get(PLUGIN_ID, VERSION).status is PluginStatus.REMOVED
        assert uow.agent_plugin_desired_versions.get(NODE_ID, PLUGIN_ID) is None
