"""M3 Master V2 execution.ack 和 lease.renew 测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from aetp_protocol.ids import MessageId, SessionId, Sha256, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionAck, LeaseRenewed, LeaseRenewRequest
from aetp_protocol.topics import v2_event_topic
from aetp_protocol.v2_envelope import V2Envelope, V2Sender, parse_v2_message

from common.transport import MqttMessage
from master.application.services.plan_lease_service import with_plan_hash
from master.domain.enums import NodeStatus
from master.domain.models import Node, NodeSession
from tests.test_m3_plan_lease import NODE_ID, NOW, SESSION_ID, _plan

MASTER_ID = stable_id("aetp-master")


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


def _agent_message(
    message_type: MessageType,
    payload,
    *,
    message_id: MessageId,
    correlation_id: MessageId | None = None,
) -> MqttMessage:
    envelope = V2Envelope(
        message_id=message_id,
        correlation_id=correlation_id,
        sent_at=NOW,
        sender=V2Sender(kind="agent", id=NODE_ID, session_id=SESSION_ID),
        message_type=message_type.value,
        trace_id=TraceId(new_id()),
        payload=payload.model_dump(mode="json"),
    )
    return MqttMessage(
        topic=v2_event_topic(
            NODE_ID.root,
            {MessageType.EXECUTION_ACK: "execution.ack", MessageType.LEASE_RENEW: "lease.renew"}[message_type],
        ),
        payload=envelope.model_dump_json().encode("utf-8"),
    )


def _future_plan():
    now = datetime.now(UTC)
    base = _plan().model_copy(
        update={
            "created_at": now - timedelta(seconds=1),
            "deadline_at": now + timedelta(hours=1),
            "resource_bindings": tuple(
                binding.model_copy(update={"expires_at": now + timedelta(minutes=5)})
                for binding in _plan().resource_bindings
            ),
        }
    )
    return with_plan_hash(base)


def test_master_router_projects_v2_execution_ack_against_stored_plan(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    plan = _future_plan()
    container.plan_lease_service().allocate(plan)
    ack = ExecutionAck(
        run_id=plan.run_id,
        shard_id=plan.shard_id,
        attempt_id=plan.attempt_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        accepted=True,
    )
    message = _agent_message(
        MessageType.EXECUTION_ACK,
        ack,
        message_id=MessageId("execution-ack-0001"),
        correlation_id=MessageId("execution-plan-0001"),
    )

    assert asyncio.run(container.message_router().handle(message)) is True
    tampered = ack.model_copy(update={"plan_hash": Sha256("0" * 64)})
    assert asyncio.run(
        container.message_router().handle(
            _agent_message(
                MessageType.EXECUTION_ACK,
                tampered,
                message_id=MessageId("execution-ack-0002"),
                correlation_id=MessageId("execution-plan-0002"),
            )
        )
    ) is False


def test_master_router_renews_lease_and_replays_duplicate_message(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    plan = _future_plan()
    container.plan_lease_service().allocate(plan)
    binding = plan.resource_bindings[0]
    request = LeaseRenewRequest(
        plan_id=plan.plan_id,
        attempt_id=plan.attempt_id,
        lease_id=binding.lease_id,
        revision=1,
        requested_expires_at=datetime.now(UTC) + timedelta(minutes=4),
    )
    message_id = MessageId("lease-renew-000001")
    message = _agent_message(MessageType.LEASE_RENEW, request, message_id=message_id)

    assert asyncio.run(container.message_router().handle(message)) is True
    with container.uow_factory()() as uow:
        outbox_id = stable_id(f"lease-renewed:{message_id.root}").root
        outbox = uow.outbox_messages.get_by_outbox_id(outbox_id)
        assert outbox is not None
        _, response = parse_v2_message(outbox.payload)
        assert isinstance(response, LeaseRenewed)
        assert response.accepted is True
        assert response.revision == 2
        assert response.expires_at == request.requested_expires_at
        lease = uow.resource_leases.get_by_lease_id(binding.lease_id)
        assert lease is not None
        assert lease.lease.revision == 2

    assert asyncio.run(container.message_router().handle(message)) is True
    with container.uow_factory()() as uow:
        lease = uow.resource_leases.get_by_lease_id(binding.lease_id)
        assert lease is not None
        assert lease.lease.revision == 2


def test_master_router_rejects_old_session_lease_renewal(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    plan = _future_plan()
    container.plan_lease_service().allocate(plan)
    binding = plan.resource_bindings[0]
    request = LeaseRenewRequest(
        plan_id=plan.plan_id,
        attempt_id=plan.attempt_id,
        lease_id=binding.lease_id,
        revision=1,
        requested_expires_at=datetime.now(UTC) + timedelta(minutes=4),
    )
    message = _agent_message(
        MessageType.LEASE_RENEW,
        request,
        message_id=MessageId("lease-renew-000002"),
    )
    message = MqttMessage(
        topic=message.topic,
        payload=V2Envelope(
            message_id=MessageId("lease-renew-000002"),
            sent_at=NOW,
            sender=V2Sender(kind="agent", id=NODE_ID, session_id=SessionId("session-00000002")),
            message_type=MessageType.LEASE_RENEW.value,
            trace_id=TraceId(new_id()),
            payload=request.model_dump(mode="json"),
        ).model_dump_json().encode("utf-8"),
    )

    assert asyncio.run(container.message_router().handle(message)) is True
    with container.uow_factory()() as uow:
        outbox_id = stable_id("lease-renewed:lease-renew-000002").root
        outbox = uow.outbox_messages.get_by_outbox_id(outbox_id)
        assert outbox is not None
        _, response = parse_v2_message(outbox.payload)
        assert isinstance(response, LeaseRenewed)
        assert response.accepted is False
        assert response.code is not None and response.code.root == "STALE_SESSION"
        lease = uow.resource_leases.get_by_lease_id(binding.lease_id)
        assert lease is not None
        assert lease.lease.revision == 1
