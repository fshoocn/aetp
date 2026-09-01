"""M2 节点能力和诊断快照仓储测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aetp_protocol.capabilities import (
    AgentMaintenanceState,
    NodeCapabilitySnapshot,
)
from aetp_protocol.ids import BusinessId, RequestId, SemVer, SessionId, Sha256, Version
from aetp_protocol.payloads import (
    ActiveAttemptInfo,
    AgentSystemInfo,
    DiagnosticsSnapshot,
    MqttConnectionInfo,
)

from master.domain.enums import NodeStatus
from master.domain.models import (
    AgentDiagnosticsSnapshotRecord,
    Node,
    NodeCapabilitySnapshotRecord,
)

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
NEW_SESSION_ID = SessionId("session-00000002")


def _register_node(container) -> None:
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
            master_node_session(node.id, SESSION_ID.root),
        )


def master_node_session(node_pk: int, session_id: str):
    from master.domain.models import NodeSession

    return NodeSession(
        node_pk=node_pk,
        node_id=NODE_ID.root,
        session_id=session_id,
        client_id="aetp-agent-bench-01",
        connected_at=datetime.now(UTC),
    )


def _snapshot(revision: int, session_id: SessionId = SESSION_ID) -> NodeCapabilitySnapshot:
    return NodeCapabilitySnapshot(
        schema_version=2,
        node_id=NODE_ID,
        session_id=session_id,
        revision=revision,
        reported_at=datetime.now(UTC),
        maintenance_state=AgentMaintenanceState.IDLE,
    )


def _diagnostics(request_id: str, collected_at: datetime) -> DiagnosticsSnapshot:
    return DiagnosticsSnapshot(
        request_id=RequestId(request_id),
        node_id=NODE_ID,
        collected_at=collected_at,
        maintenance_state=AgentMaintenanceState.IDLE,
        system=AgentSystemInfo(
            hostname="bench-01",
            os_name="windows",
            os_version="10.0",
            process_id=1234,
            agent_started_at=collected_at - timedelta(minutes=5),
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
            broker_endpoint="mqtt.example.test:8883",
            reconnect_count=0,
        ),
        plugins=(),
        active_attempts=(
            ActiveAttemptInfo(
                attempt_id=BusinessId("01J00000000000000000000001"),
                plan_id=BusinessId("01J00000000000000000000002"),
                run_id=BusinessId("01J00000000000000000000003"),
                state="running",
                started_at=collected_at,
            ),
        ),
        capability_revision=1,
    )


def test_capability_snapshot_repository_rejects_non_newer_revision(client) -> None:
    container = client.app.state.container
    _register_node(container)
    now = datetime.now(UTC)

    with container.uow_factory()() as uow:
        first = NodeCapabilitySnapshotRecord(
            id=None,
            node_id=NODE_ID,
            session_id=SESSION_ID,
            revision=1,
            snapshot_sha256=Sha256("a" * 64),
            snapshot=_snapshot(1),
            reported_at=now,
            created_at=now,
        )
        assert uow.node_capability_snapshots.add_if_newer(first) is True
        assert uow.node_capability_snapshots.add_if_newer(first) is False
        with pytest.raises(ValueError, match="摘要冲突"):
            uow.node_capability_snapshots.add_if_newer(
                NodeCapabilitySnapshotRecord(
                    id=None,
                    node_id=NODE_ID,
                    session_id=SESSION_ID,
                    revision=1,
                    snapshot_sha256=Sha256("f" * 64),
                    snapshot=_snapshot(1),
                    reported_at=now,
                    created_at=now,
                )
            )
        assert uow.node_capability_snapshots.add_if_newer(
            NodeCapabilitySnapshotRecord(
                id=None,
                node_id=NODE_ID,
                session_id=SESSION_ID,
                revision=2,
                snapshot_sha256=Sha256("b" * 64),
                snapshot=_snapshot(2),
                reported_at=now,
                created_at=now,
            )
        ) is True
        assert uow.node_capability_snapshots.add_if_newer(
            NodeCapabilitySnapshotRecord(
                id=None,
                node_id=NODE_ID,
                session_id=NEW_SESSION_ID,
                revision=1,
                snapshot_sha256=Sha256("c" * 64),
                snapshot=_snapshot(1, NEW_SESSION_ID),
                reported_at=now,
                created_at=now,
            )
        ) is True

    with container.uow_factory()() as uow:
        latest = uow.node_capability_snapshots.get_latest(NODE_ID)
        assert latest is not None
        assert latest.session_id == NEW_SESSION_ID
        assert latest.revision == 1
        assert latest.snapshot.node_id == NODE_ID
        assert len(uow.node_capability_snapshots.list_by_node(NODE_ID)) == 3


def test_diagnostics_repository_is_typed_and_request_idempotent(client) -> None:
    container = client.app.state.container
    _register_node(container)
    collected_at = datetime.now(UTC)
    snapshot = _diagnostics("request-00000001", collected_at)
    record = AgentDiagnosticsSnapshotRecord(
        id=None,
        request_id=snapshot.request_id,
        node_id=NODE_ID,
        session_id=SESSION_ID,
        snapshot=snapshot,
        collected_at=collected_at,
        created_at=collected_at,
    )

    with container.uow_factory()() as uow:
        added = uow.agent_diagnostics_snapshots.add(record)
        repeated = uow.agent_diagnostics_snapshots.add(record)
        assert repeated.id == added.id
        assert repeated.snapshot.system.hostname == "bench-01"
        assert uow.agent_diagnostics_snapshots.get_latest(NODE_ID).request_id == snapshot.request_id

        with pytest.raises(ValueError, match="不同快照"):
            uow.agent_diagnostics_snapshots.add(
                AgentDiagnosticsSnapshotRecord(
                    id=None,
                    request_id=snapshot.request_id,
                    node_id=NODE_ID,
                    session_id=SESSION_ID,
                    snapshot=_diagnostics("request-00000002", collected_at),
                    collected_at=collected_at,
                    created_at=collected_at,
                )
            )
