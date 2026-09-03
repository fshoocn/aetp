""" 插件同步 Master/Agent 消息闭环测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from aetp_protocol.envelope import Envelope, Sender, parse_message
from aetp_protocol.ids import BusinessId, MessageId, PluginId, SemVer, SessionId, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import MaintenanceStatus
from aetp_protocol.plugin_types import PluginDistributionRef, PluginSyncAction
from aetp_protocol.plugins import PluginSyncItem, PluginSyncResult
from aetp_protocol.topics import event_topic
from pydantic import BaseModel

from common.transport import MqttMessage
from master.domain.enums import NodeStatus
from master.domain.models import Node, NodeSession
from master.domain.models.plugin_governance import PluginSyncOperationState
from tests.test_plugin_archive import _archive

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
PLUGIN_ID = PluginId("org.example.executor")
VERSION = SemVer("2.0.0")
SYNC_ID = BusinessId("01J00000000000000000000001")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _agent_envelope(
    message_type: MessageType,
    payload: BaseModel,
    *,
    correlation_id: MessageId | None = None,
) -> bytes:
    envelope = Envelope(
        message_id=MessageId(new_id()),
        correlation_id=correlation_id,
        sent_at=NOW,
        sender=Sender(kind="agent", id=NODE_ID, session_id=SESSION_ID),
        message_type=message_type.value,
        trace_id=TraceId(new_id()),
        payload=payload.model_dump(mode="json"),
    )
    return envelope.model_dump_json().encode("utf-8")


def _seed_node(container) -> None:
    with container.uow_factory()() as uow:
        node = uow.nodes.save(
            Node(
                id=None,
                node_id=NODE_ID.root,
                name="Bench 01",
                hostname="bench-01",
                status=NodeStatus.ONLINE,
                online=True,
                enabled=True,
            )
        )
        assert node.id is not None
        uow.node_sessions.create(
            NodeSession(
                node_pk=node.id,
                node_id=NODE_ID.root,
                session_id=SESSION_ID.root,
                client_id="aetp-agent-bench-01",
                connected_at=NOW,
            )
        )


def _admin_headers(client) -> dict[str, str]:
    service = client.app.state.container.auth_service()
    assert service.bootstrap_admin("sync-admin", "admin-pass-123", "Sync Admin")
    response = client.post(
        "/api/v2/auth/login",
        json={"username": "sync-admin", "password": "admin-pass-123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_master_plugin_sync_command_and_agent_result_close_loop(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    archive_content = _archive()
    archive = container.plugin_governance_service().register_archive("example.zip", archive_content)
    request = container.plugin_sync_service().request(
        NODE_ID,
        (
            PluginSyncItem(
                plugin_id=PLUGIN_ID,
                version=VERSION,
                action=PluginSyncAction.INSTALL,
                package=PluginDistributionRef(
                    plugin_id=PLUGIN_ID,
                    version=VERSION,
                    archive_sha256=archive.archive_sha256,
                ),
            ),
        ),
    )

    with container.uow_factory()() as uow:
        outbox_id = stable_id(f"plugin-sync:{request.sync_id.root}").root
        outbox = uow.outbox_messages.get_by_outbox_id(outbox_id)
        assert outbox is not None
        command_envelope, command_payload = parse_message(outbox.payload)
        assert command_envelope.message_type == MessageType.AGENT_PLUGIN_SYNC.value
        assert command_payload.sync_id == request.sync_id
        assert command_payload.items[0].package is not None
        assert command_payload.items[0].package.download_url is not None
        download = client.get(command_payload.items[0].package.download_url)
        assert download.status_code == 200
        assert download.content == archive_content
        assert uow.agent_plugin_sync_operations.get(request.sync_id).state is PluginSyncOperationState.DRAINING

    status = MaintenanceStatus(
        node_id=NODE_ID,
        sequence=1,
        state="updating",
        sync_id=request.sync_id,
        active_attempt_count=0,
        occurred_at=NOW,
    )
    status_message = MqttMessage(
        topic=event_topic(NODE_ID.root, "agent.maintenance.status"),
        payload=_agent_envelope(MessageType.AGENT_MAINTENANCE_STATUS, status),
    )
    assert asyncio.run(container.message_router().handle(status_message)) is True

    result = PluginSyncResult(
        sync_id=request.sync_id,
        node_id=NODE_ID,
        accepted=True,
        restart_required=True,
        items=(
            {
                "plugin_id": PLUGIN_ID,
                "version": VERSION,
                "state": "active",
            },
        ),
    )
    result_message = MqttMessage(
        topic=event_topic(NODE_ID.root, "agent.plugin.sync.result"),
        payload=_agent_envelope(
            MessageType.AGENT_PLUGIN_SYNC_RESULT,
            result,
            correlation_id=command_envelope.message_id,
        ),
    )
    assert asyncio.run(container.message_router().handle(result_message)) is True
    assert asyncio.run(container.message_router().handle(result_message)) is True

    with container.uow_factory()() as uow:
        operation = uow.agent_plugin_sync_operations.get(request.sync_id)
        assert operation is not None
        assert operation.state is PluginSyncOperationState.SUCCEEDED
        assert operation.accepted is True
        assert operation.restart_required is True
        assert operation.results is not None
        assert operation.results[0].state == "active"

    _, status_payload = parse_message(json.loads(status_message.payload))
    assert isinstance(status_payload, MaintenanceStatus)


def test_plugin_sync_api_is_admin_only_and_enqueues_command(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    archive = container.plugin_governance_service().register_archive("example.zip", _archive())
    item = {
        "plugin_id": PLUGIN_ID.root,
        "version": VERSION.root,
        "action": "install",
        "package": {
            "plugin_id": PLUGIN_ID.root,
            "version": VERSION.root,
            "archive_sha256": archive.archive_sha256.root,
        },
    }

    unauthorized = client.post(
        f"/api/v2/nodes/{NODE_ID.root}/plugin-sync",
        json={"items": [item]},
    )
    assert unauthorized.status_code == 401

    response = client.post(
        f"/api/v2/nodes/{NODE_ID.root}/plugin-sync",
        headers=_admin_headers(client),
        json={"items": [item], "restart_after": False},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["node_id"] == NODE_ID.root
    assert body["state"] == "draining"
    assert body["restart_required"] is False
