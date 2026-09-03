"""M1 插件治理数据库仓储测试。"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, PluginId, SemVer, SessionId, Sha256
from aetp_protocol.plugin_types import (
    DesiredPluginVersion,
    EntrypointRef,
    PluginDistributionRef,
    PluginPoint,
    PluginStatus,
    PluginSyncAction,
)
from aetp_protocol.plugins import (
    PluginEntrypoints,
    PluginManifest,
    PluginSyncItem,
    PluginSyncItemResult,
    PluginSyncRequest,
    PluginSyncResult,
)

from master.application.services.plugin_governance_service import PluginGovernanceService
from master.application.services.plugin_sync_service import (
    InvalidPluginSyncTransition,
    PluginSyncService,
)
from master.domain.enums import NodeStatus
from master.domain.models import (
    AgentPluginDesiredVersionRecord,
    AgentPluginSyncOperationRecord,
    Node,
    PluginSyncOperationState,
    PluginVersionRecord,
)
from master.plugins.registry import PluginRegistry
from tests.test_plugin_archive import _archive

NODE_ID = BusinessId("01J00000000000000000000000")
PLUGIN_ID = PluginId("org.example.executor")
VERSION = SemVer("2.0.0")
SESSION_ID = SessionId("session-00000001")
SYNC_ID = BusinessId("01J00000000000000000000001")


def _manifest() -> PluginManifest:
    return PluginManifest(
        schema_version=2,
        id=PLUGIN_ID,
        version=VERSION,
        api_version=SemVer("2.0.0"),
        point=PluginPoint.EXECUTOR,
        display_name="Example Executor",
        entrypoints=PluginEntrypoints(
            master=EntrypointRef("plugin:create_plugin"),
            agent=EntrypointRef("plugin:create_plugin"),
        ),
    )


def test_plugin_governance_repositories_round_trip(client) -> None:
    container = client.app.state.container
    now = datetime.now(UTC)

    with container.uow_factory()() as uow:
        plugin = uow.plugin_versions.add(
            PluginVersionRecord(
                id=None,
                plugin_id=PLUGIN_ID,
                version=VERSION,
                point=PluginPoint.EXECUTOR,
                status=PluginStatus.VERIFIED,
                filename="example.zip",
                archive_sha256=Sha256("a" * 64),
                manifest_sha256=Sha256("b" * 64),
                manifest=_manifest(),
                archive_path="plugins/versions/org.example.executor/2.0.0/example.zip",
                installed_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        desired = uow.agent_plugin_desired_versions.upsert(
            AgentPluginDesiredVersionRecord(
                id=None,
                node_id=NODE_ID,
                desired=DesiredPluginVersion(
                    plugin_id=PLUGIN_ID,
                    point=PluginPoint.EXECUTOR,
                    version=VERSION,
                ),
                created_at=now,
                updated_at=now,
            )
        )
        sync = uow.agent_plugin_sync_operations.add(
            AgentPluginSyncOperationRecord(
                id=None,
                sync_id=SYNC_ID,
                node_id=NODE_ID,
                expected_session_id=SESSION_ID,
                state=PluginSyncOperationState.PENDING,
                items=(
                    PluginSyncItem(
                        plugin_id=PLUGIN_ID,
                        version=VERSION,
                        action="install",
                        package=PluginDistributionRef(
                            plugin_id=PLUGIN_ID,
                            version=VERSION,
                            archive_sha256=Sha256("a" * 64),
                        ),
                    ),
                ),
                results=None,
                accepted=None,
                restart_required=True,
                error_code=None,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
        )

        assert plugin.id is not None
        assert uow.plugin_versions.get(PLUGIN_ID, VERSION).manifest.id == PLUGIN_ID
        assert uow.plugin_versions.get_by_archive_sha256(Sha256("a" * 64)).id == plugin.id
        assert uow.agent_plugin_desired_versions.get(NODE_ID, PLUGIN_ID).desired.version == VERSION
        assert uow.agent_plugin_desired_versions.list_by_node(NODE_ID)[0].id == desired.id
        assert uow.agent_plugin_sync_operations.get(SYNC_ID).state is PluginSyncOperationState.PENDING
        assert uow.agent_plugin_sync_operations.list_by_node(NODE_ID)[0].id == sync.id


def test_sync_operation_update_preserves_typed_result(client) -> None:
    container = client.app.state.container
    now = datetime.now(UTC)
    with container.uow_factory()() as uow:
        record = uow.agent_plugin_sync_operations.add(
            AgentPluginSyncOperationRecord(
                id=None,
                sync_id=SYNC_ID,
                node_id=NODE_ID,
                expected_session_id=SESSION_ID,
                state=PluginSyncOperationState.PENDING,
                items=(),
                results=None,
                accepted=None,
                restart_required=False,
                error_code=None,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        updated = uow.agent_plugin_sync_operations.update(
            AgentPluginSyncOperationRecord(
                id=record.id,
                sync_id=SYNC_ID,
                node_id=NODE_ID,
                expected_session_id=SESSION_ID,
                state=PluginSyncOperationState.FAILED,
                items=(),
                results=(),
                accepted=False,
                restart_required=False,
                error_code=ErrorCode("PLUGIN_SYNC_FAILED"),
                completed_at=now,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )

        assert updated.state is PluginSyncOperationState.FAILED
        assert updated.accepted is False
        assert updated.error_code == ErrorCode("PLUGIN_SYNC_FAILED")
        assert updated.results == ()


def test_plugin_governance_service_registers_immutable_archive_and_lifecycle(client, tmp_path) -> None:
    container = client.app.state.container
    service = PluginGovernanceService(container.uow_factory(), tmp_path / "plugins")
    content = _archive()

    registered = service.register_archive("example.zip", content)
    repeated = service.register_archive("example.zip", content)

    assert repeated.id == registered.id
    assert Path(registered.archive_path).read_bytes() == content
    assert len(list((tmp_path / "plugins").rglob("*.zip"))) == 1

    installed = service.install(PLUGIN_ID, VERSION)
    pending = service.request_enabled(PLUGIN_ID, VERSION)
    enabled = service.complete_restart(PLUGIN_ID, VERSION, enabled=True)
    active_path = tmp_path / "plugins" / "active" / PLUGIN_ID.root / "active.json"
    assert '"version":"2.0.0"' in active_path.read_text(encoding="utf-8")
    with container.uow_factory()() as uow:
        registry = PluginRegistry(tmp_path / "plugins")
        registry_record = uow.plugin_versions.get(PLUGIN_ID, VERSION)
        assert registry_record is not None
        registry.load([registry_record])
        assert registry.get(PLUGIN_ID, VERSION, PluginPoint.EXECUTOR) is not None
    disabled_pending = service.request_disabled(PLUGIN_ID, VERSION)
    disabled = service.complete_restart(PLUGIN_ID, VERSION, enabled=False)
    removed = service.remove(PLUGIN_ID, VERSION)

    assert installed.status is PluginStatus.INSTALLED
    assert pending.status is PluginStatus.PENDING_RESTART
    assert enabled.status is PluginStatus.ENABLED
    assert disabled_pending.status is PluginStatus.PENDING_RESTART
    assert disabled.status is PluginStatus.DISABLED
    assert removed.status is PluginStatus.REMOVED
    assert not active_path.exists()


def test_plugin_sync_service_is_idempotent_and_records_result(client, tmp_path) -> None:
    container = client.app.state.container
    governance = PluginGovernanceService(container.uow_factory(), tmp_path / "plugins")
    registered = governance.register_archive("example.zip", _archive())
    service = PluginSyncService(container.uow_factory())

    desired = service.set_desired_version(
        NODE_ID,
        DesiredPluginVersion(
            plugin_id=PLUGIN_ID,
            point=PluginPoint.EXECUTOR,
            version=VERSION,
        ),
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
                package=PluginDistributionRef(
                    plugin_id=PLUGIN_ID,
                    version=VERSION,
                    archive_sha256=registered.archive_sha256,
                ),
            ),
        ),
    )
    operation = service.create_sync_operation(request)
    repeated = service.create_sync_operation(request)
    result = service.record_result(
        PluginSyncResult(
            sync_id=SYNC_ID,
            node_id=NODE_ID,
            accepted=True,
            restart_required=True,
            items=(
                PluginSyncItemResult(
                    plugin_id=PLUGIN_ID,
                    version=VERSION,
                    state="active",
                ),
            ),
        )
    )

    assert desired.desired.version == VERSION
    assert repeated.id == operation.id
    assert result.state is PluginSyncOperationState.SUCCEEDED
    assert result.results is not None and result.results[0].state == "active"
    with pytest.raises(InvalidPluginSyncTransition):
        service.transition(SYNC_ID, PluginSyncOperationState.CANCELLED)


def test_plugin_list_api_uses_governance_service(client, auth_header) -> None:
    response = client.get("/api/v2/plugins", headers=auth_header)

    assert response.status_code == 200
    assert response.json() == []


def _versioned_archive(version: str) -> bytes:
    files = {
        "plugin.json": json.dumps(
            {
                "schema_version": 2,
                "id": "org.example.executor",
                "version": version,
                "api_version": "2.0.0",
                "point": "executor",
                "display_name": "Example Executor",
                "entrypoints": {
                    "master": "plugin:create_plugin",
                    "agent": "plugin:create_plugin",
                },
            }
        ).encode(),
        "master/plugin.py": b"def create_plugin(): pass",
        "agent/plugin.py": b"def create_plugin(): pass",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_plugin_governance_auto_disables_previous_and_rolls_back(client, tmp_path) -> None:
    container = client.app.state.container
    service = PluginGovernanceService(container.uow_factory(), tmp_path / "plugins")
    first = service.register_archive("first.zip", _versioned_archive("2.0.0"))
    second = service.register_archive("second.zip", _versioned_archive("2.1.0"))

    service.install(first.plugin_id, first.version)
    service.request_enabled(first.plugin_id, first.version)
    enabled_first = service.complete_restart(first.plugin_id, first.version, enabled=True)
    service.install(second.plugin_id, second.version)
    service.request_enabled(second.plugin_id, second.version)
    enabled_second = service.complete_restart(second.plugin_id, second.version, enabled=True)
    rolled_back = service.rollback(first.plugin_id, first.version)

    assert enabled_first.status is PluginStatus.ENABLED
    assert enabled_second.status is PluginStatus.ENABLED
    assert rolled_back.status is PluginStatus.ENABLED
    with container.uow_factory()() as uow:
        first_record = uow.plugin_versions.get(first.plugin_id, first.version)
        second_record = uow.plugin_versions.get(second.plugin_id, second.version)
        assert first_record is not None and first_record.status is PluginStatus.ENABLED
        assert second_record is not None and second_record.status is PluginStatus.DISABLED
    pointer = (tmp_path / "plugins" / "active" / first.plugin_id.root / "active.json").read_text(encoding="utf-8")
    assert '"version":"2.0.0"' in pointer


def test_plugin_sync_service_sets_desired_version_for_node_group(client, tmp_path) -> None:
    container = client.app.state.container
    governance = PluginGovernanceService(container.uow_factory(), tmp_path / "plugins")
    registered = governance.register_archive("group.zip", _versioned_archive("2.0.0"))
    nodes = (
        "01J00000000000000000000080",
        "01J00000000000000000000081",
        "01J00000000000000000000082",
    )
    with container.uow_factory()() as uow:
        for index, node_id in enumerate(nodes):
            uow.nodes.save(
                Node(
                    id=None,
                    node_id=node_id,
                    name=f"bench-{index}",
                    hostname=f"bench-{index}",
                    status=NodeStatus.OFFLINE,
                    online=False,
                    enabled=True,
                    tags=["bench", "nightly"] if index < 2 else ["bench"],
                )
            )
    desired = DesiredPluginVersion(
        plugin_id=registered.plugin_id,
        point=registered.point,
        version=registered.version,
    )
    service = PluginSyncService(container.uow_factory())
    grouped = service.set_desired_version_for_tag("nightly", desired)
    selected = service.set_desired_version_for_nodes(
        (BusinessId(nodes[0]), BusinessId(nodes[0]), BusinessId(nodes[2])),
        desired,
    )

    assert len(grouped) == 2
    assert len(selected) == 2
    assert {item.node_id.root for item in grouped} == {nodes[0], nodes[1]}
    assert {item.node_id.root for item in selected} == {nodes[0], nodes[2]}


def test_node_group_desired_plugin_api(client) -> None:
    container = client.app.state.container
    auth = container.auth_service()
    assert auth.bootstrap_admin("desired-api-admin", "admin-pass-123", "Desired API Admin")
    login = client.post(
        "/api/v2/auth/login",
        json={"username": "desired-api-admin", "password": "admin-pass-123"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    registered = container.plugin_governance_service().register_archive(
        "desired-api.zip",
        _versioned_archive("2.0.0"),
    )
    with container.uow_factory()() as uow:
        for index, node_id in enumerate(("01J00000000000000000000083", "01J00000000000000000000084")):
            uow.nodes.save(
                Node(
                    id=None,
                    node_id=node_id,
                    name=f"api-bench-{index}",
                    hostname=f"api-bench-{index}",
                    status=NodeStatus.OFFLINE,
                    online=False,
                    enabled=True,
                    tags=["nightly"],
                )
            )

    response = client.put(
        "/api/v2/nodes/groups/nightly/desired-plugin",
        headers=headers,
        json={
            "plugin_id": registered.plugin_id.root,
            "point": registered.point.value,
            "version": registered.version.root,
        },
    )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 2
    assert {item["desired"]["version"] for item in response.json()} == {"2.0.0"}
