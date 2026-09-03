"""M5 Master 远程维护操作、维护锁和 API 测试。"""

from __future__ import annotations

import asyncio
import json

import pytest
from aetp_protocol.envelope import Envelope, Sender
from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, MessageId, SessionId, TraceId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import MaintenanceDrainResult, MaintenanceRestartResult, RemoteOperationStatus
from aetp_protocol.topics import event_topic

from common.transport import MqttMessage
from master.application.services.agent_maintenance_service import MaintenanceLockConflict
from master.domain.enums import PlatformRole
from tests.test_m3_plan_lease import NODE_ID, SESSION_ID, _seed_node


def _result_message(
    *,
    operation_id: BusinessId,
    accepted: bool,
    session_id: SessionId = SESSION_ID,
    restart: bool = False,
) -> MqttMessage:
    payload = (
        MaintenanceRestartResult(
            node_id=NODE_ID,
            operation_id=operation_id,
            accepted=accepted,
            code=None if accepted else ErrorCode("AGENT_MAINTENANCE"),
            message="ok" if accepted else "still busy",
        )
        if restart
        else MaintenanceDrainResult(
            node_id=NODE_ID,
            operation_id=operation_id,
            accepted=accepted,
            active_attempt_count=0 if accepted else 2,
            code=None if accepted else ErrorCode("AGENT_MAINTENANCE"),
            message="ok" if accepted else "still busy",
        )
    )
    message_type = (
        MessageType.AGENT_MAINTENANCE_RESTART_RESULT
        if restart
        else MessageType.AGENT_MAINTENANCE_DRAIN_RESULT
    )
    topic_segment = (
        "agent.maintenance.restart.result"
        if restart
        else "agent.maintenance.drain.result"
    )
    envelope = Envelope(
        message_id=MessageId("m5-maintenance-result-01"),
        sent_at="2026-09-02T08:00:00Z",
        sender=Sender(kind="agent", id=NODE_ID, session_id=session_id),
        message_type=message_type.value,
        trace_id=TraceId("m5-maintenance-trace-01"),
        payload=payload.model_dump(mode="json"),
    )
    return MqttMessage(
        topic=event_topic(NODE_ID.root, topic_segment),
        payload=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
    )


def test_maintenance_request_uses_transactional_lock_and_outbox(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    service = container.agent_maintenance_service()

    operation = service.request_drain(NODE_ID, drain_timeout_s=30, reason="bench update")

    assert operation.status is RemoteOperationStatus.PENDING
    with container.uow_factory()() as uow:
        lock = uow.maintenance_locks.get(NODE_ID)
        assert lock is not None
        assert lock.operation_id == operation.operation_id
        outbox = uow.outbox_messages.get_by_outbox_id(
            stable_id(f"agent-maintenance:{operation.operation_id.root}").root
        )
        assert outbox is not None
        assert outbox.topic.endswith("/agent.maintenance.drain")

    with pytest.raises(MaintenanceLockConflict):
        service.request_restart(NODE_ID, drain_timeout_s=0)


def test_router_records_maintenance_result_and_releases_failed_lock(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    service = container.agent_maintenance_service()
    operation = service.request_drain(NODE_ID, drain_timeout_s=0)

    assert asyncio.run(container.message_router().handle(
        _result_message(operation_id=operation.operation_id, accepted=False)
    )) is True

    stored = service.get_operation(operation.operation_id)
    assert stored is not None
    assert stored.status is RemoteOperationStatus.FAILED
    with container.uow_factory()() as uow:
        assert uow.maintenance_locks.get(NODE_ID) is None


def test_new_session_releases_successful_restart_lock(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    service = container.agent_maintenance_service()
    operation = service.request_restart(NODE_ID, drain_timeout_s=0)
    assert asyncio.run(container.message_router().handle(
        _result_message(
            operation_id=operation.operation_id,
            accepted=True,
            restart=True,
        )
    )) is True

    with container.uow_factory()() as uow:
        assert uow.maintenance_locks.get(NODE_ID) is not None
    service.on_session_registered(NODE_ID, SessionId("session-00000200"))
    with container.uow_factory()() as uow:
        assert uow.maintenance_locks.get(NODE_ID) is None


def test_node_maintenance_api_creates_and_reads_operation(client, auth_header) -> None:
    container = client.app.state.container
    _seed_node(container)
    with container.uow_factory()() as uow:
        user = uow.users.get_by_username("tester")
        assert user is not None
        user.platform_role = PlatformRole.ADMIN
        uow.users.update(user)

    response = client.post(
        f"/api/v2/nodes/{NODE_ID.root}/log-level",
        headers=auth_header,
        json={"component": "agent.runtime", "level": "debug"},
    )
    assert response.status_code == 202
    operation_id = response.json()["operation_id"]
    assert response.json()["kind"] == "log_level"
    with container.uow_factory()() as uow:
        audit = uow.audit_logs.list(action="agent.maintenance.log_level")
        assert len(audit) == 1
        assert audit[0].actor_id is not None
        assert audit[0].detail["component"] == "agent.runtime"

    detail = client.get(
        f"/api/v2/nodes/{NODE_ID.root}/maintenance/operations/{operation_id}",
        headers=auth_header,
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "pending"

    logs = client.get(
        f"/api/v2/nodes/{NODE_ID.root}/logs?limit=10",
        headers=auth_header,
    )
    assert logs.status_code == 200
    assert logs.json() == []
