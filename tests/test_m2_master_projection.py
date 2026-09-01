"""M2 Master V2 能力与诊断事件路由测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aetp_protocol.capabilities import AgentMaintenanceState, NodeCapabilitySnapshot
from aetp_protocol.ids import BusinessId, RequestId, SemVer, SessionId, TraceId, Version, new_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    ActiveAttemptInfo,
    AgentSystemInfo,
    DiagnosticsSnapshot,
    MqttConnectionInfo,
)
from aetp_protocol.topics import v2_event_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender

from common.transport import MqttMessage
from master.domain.enums import NodeStatus
from master.domain.models import Node, NodeSession

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
OLD_SESSION_ID = SessionId("session-00000002")
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


def _envelope(message_type: MessageType, session_id: SessionId, payload: object) -> bytes:
    envelope = V2Envelope(
        message_id=new_id(),
        sent_at=NOW,
        sender=V2Sender(kind="agent", id=NODE_ID, session_id=session_id),
        message_type=message_type.value,
        trace_id=TraceId(new_id()),
        payload=payload.model_dump(mode="json"),
    )
    return envelope.model_dump_json().encode("utf-8")


def _capability(revision: int, session_id: SessionId = SESSION_ID) -> NodeCapabilitySnapshot:
    return NodeCapabilitySnapshot(
        schema_version=2,
        node_id=NODE_ID,
        session_id=session_id,
        revision=revision,
        reported_at=NOW,
        maintenance_state=AgentMaintenanceState.IDLE,
    )


def _diagnostics(request_id: str) -> DiagnosticsSnapshot:
    return DiagnosticsSnapshot(
        request_id=RequestId(request_id),
        node_id=NODE_ID,
        collected_at=NOW,
        maintenance_state=AgentMaintenanceState.IDLE,
        system=AgentSystemInfo(
            hostname="bench-01",
            os_name="windows",
            os_version="10.0",
            process_id=1234,
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
        active_attempts=(
            ActiveAttemptInfo(
                attempt_id=BusinessId("01J00000000000000000000001"),
                plan_id=BusinessId("01J00000000000000000000002"),
                run_id=BusinessId("01J00000000000000000000003"),
                state="running",
            ),
        ),
        capability_revision=1,
    )


def test_master_router_accepts_idempotent_current_session_snapshot(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    router = container.message_router()
    message = MqttMessage(
        topic=v2_event_topic(NODE_ID.root, "capability.snapshot"),
        payload=_envelope(MessageType.NODE_CAPABILITY_SNAPSHOT, SESSION_ID, _capability(1)),
    )

    assert asyncio.run(router.handle(message)) is True
    assert asyncio.run(router.handle(message)) is True

    with container.uow_factory()() as uow:
        records = uow.node_capability_snapshots.list_by_node(NODE_ID)
        assert len(records) == 1
        assert records[0].revision == 1


def test_master_router_rejects_old_session_snapshot(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    router = container.message_router()
    message = MqttMessage(
        topic=v2_event_topic(NODE_ID.root, "capability.snapshot"),
        payload=_envelope(MessageType.NODE_CAPABILITY_SNAPSHOT, OLD_SESSION_ID, _capability(1, OLD_SESSION_ID)),
    )

    assert asyncio.run(router.handle(message)) is False

    with container.uow_factory()() as uow:
        assert uow.node_capability_snapshots.get_latest(NODE_ID) is None


def test_master_router_persists_diagnostics_snapshot_by_request_id(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    router = container.message_router()
    snapshot = _diagnostics("request-00000001")
    message = MqttMessage(
        topic=v2_event_topic(NODE_ID.root, "agent.diagnostics.snapshot"),
        payload=_envelope(MessageType.AGENT_DIAGNOSTICS_SNAPSHOT, SESSION_ID, snapshot),
    )

    assert asyncio.run(router.handle(message)) is True
    assert asyncio.run(router.handle(message)) is True

    with container.uow_factory()() as uow:
        record = uow.agent_diagnostics_snapshots.get_latest(NODE_ID)
        assert record is not None
        assert record.request_id == snapshot.request_id
        assert record.snapshot.system.hostname == "bench-01"
