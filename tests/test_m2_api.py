"""M2 V2 节点能力和诊断 API 测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from aetp_protocol.capabilities import AgentMaintenanceState, NodeCapabilitySnapshot
from aetp_protocol.envelope import parse_message
from aetp_protocol.ids import BusinessId, RequestId, SemVer, SessionId, Version
from aetp_protocol.payloads import AgentSystemInfo, DiagnosticsSnapshot, MqttConnectionInfo

from master.domain.enums import NodeStatus
from master.domain.models import Node, NodeSession

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


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


def _capability_snapshot() -> NodeCapabilitySnapshot:
    return NodeCapabilitySnapshot(
        schema_version=2,
        node_id=NODE_ID,
        session_id=SESSION_ID,
        revision=1,
        reported_at=NOW,
        maintenance_state=AgentMaintenanceState.IDLE,
    )


def _diagnostics_snapshot() -> DiagnosticsSnapshot:
    return DiagnosticsSnapshot(
        request_id=RequestId("request-00000001"),
        node_id=NODE_ID,
        collected_at=NOW,
        maintenance_state=AgentMaintenanceState.IDLE,
        system=AgentSystemInfo(
            hostname="bench-01",
            os_name="windows",
            os_version="10.0",
            process_id=10,
            agent_started_at=NOW,
            python_version=Version("3.14"),
            cpu_cores=8,
            memory_total_mb=16_384,
            memory_available_mb=8_192,
            disk_free_mb=100_000,
            agent_version=SemVer("2.0.0"),
            protocol_version=2,
        ),
        mqtt=MqttConnectionInfo(
            connected=True,
            broker_endpoint="broker.test:8883",
            reconnect_count=0,
        ),
        plugins=(),
        active_attempts=(),
        capability_revision=1,
    )


def test_v2_node_api_returns_latest_capability_and_diagnostics(client, auth_header) -> None:
    container = client.app.state.container
    _seed_node(container)
    assert container.capability_snapshot_service().accept(_capability_snapshot()) is True
    assert container.diagnostics_snapshot_service().accept(
        _diagnostics_snapshot(),
        sender_session_id=SESSION_ID,
    ) is True

    capability_response = client.get(
        f"/api/v2/nodes/{NODE_ID.root}/capability-snapshot",
        headers=auth_header,
    )
    diagnostics_response = client.get(
        f"/api/v2/nodes/{NODE_ID.root}/diagnostics",
        headers=auth_header,
    )

    assert capability_response.status_code == 200
    assert capability_response.json()["revision"] == 1
    assert capability_response.json()["snapshot"]["node_id"] == NODE_ID.root
    assert diagnostics_response.status_code == 200
    assert diagnostics_response.json()["snapshot"]["system"]["hostname"] == "bench-01"


def test_v2_node_api_rejects_invalid_id_and_reports_missing_snapshot(client, auth_header) -> None:
    missing = client.get(
        "/api/v2/nodes/01J00000000000000000000000/capability-snapshot",
        headers=auth_header,
    )
    invalid = client.get("/api/v2/nodes/not-a-business-id/diagnostics", headers=auth_header)

    assert missing.status_code == 404
    assert invalid.status_code == 422


def test_v2_diagnostics_collect_enqueues_typed_command(client, auth_header) -> None:
    container = client.app.state.container
    _seed_node(container)

    response = client.post(
        f"/api/v2/nodes/{NODE_ID.root}/diagnostics/collect",
        headers=auth_header,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    with container.uow_factory()() as uow:
        outbox = uow.outbox_messages.get_by_outbox_id(f"diagnostics:{body['request_id']}")
        assert outbox is not None
        envelope, payload = parse_message(outbox.payload)
        assert envelope.message_type == "agent.diagnostics.request"
        assert payload.node_id == NODE_ID
