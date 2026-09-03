"""M3 Agent Lease 续租消息测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from aetp_protocol.envelope import Envelope, Sender, parse_message
from aetp_protocol.ids import MessageId, SessionId, TraceId, new_id, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import LeaseRenewed, LeaseRenewRequest
from aetp_protocol.plan_hash import with_plan_hash
from aetp_protocol.topics import command_topic, event_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.lease_renewal_service import LeaseRenewalService
from common.transport import MqttMessage
from tests.test_agent_capability_publisher import _publisher
from tests.test_m3_plan_lease import NODE_ID, NOW, SESSION_ID, _plan


def test_agent_lease_renewal_emits_request_and_applies_master_response(tmp_path) -> None:
    publisher, _transport = _publisher(tmp_path)
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    now = NOW + timedelta(minutes=4, seconds=50)
    service = LeaseRenewalService(
        NODE_ID,
        ledger,
        publisher,
        now=lambda: now,
        renewal_lead_s=15,
        extension_s=60,
    )
    plan = with_plan_hash(_plan())
    service.register_plan(plan)

    assert asyncio.run(service.run_once(SESSION_ID)) == 1
    assert asyncio.run(service.run_once(SESSION_ID)) == 0
    outbox = ledger.claim_due_outbox(10, datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1))[0]
    request_envelope, request_payload = parse_message(outbox.payload)
    assert isinstance(request_payload, LeaseRenewRequest)
    assert outbox.topic == event_topic(NODE_ID.root, "lease.renew")
    assert request_payload.revision == 1
    assert request_payload.requested_expires_at == NOW + timedelta(minutes=5, seconds=50)

    renewed = LeaseRenewed(
        plan_id=plan.plan_id,
        attempt_id=plan.attempt_id,
        lease_id=request_payload.lease_id,
        accepted=True,
        revision=2,
        expires_at=NOW + timedelta(minutes=6),
    )
    response = Envelope(
        message_id=MessageId(new_id()),
        correlation_id=request_envelope.message_id,
        sent_at=NOW,
        sender=Sender(
            kind="master",
            id=stable_id("aetp-master"),
            session_id=SessionId("master-session-01"),
        ),
        message_type=MessageType.LEASE_RENEWED.value,
        trace_id=TraceId(new_id()),
        payload=renewed.model_dump(mode="json"),
    )

    assert service.handle_renewed(
        MqttMessage(
            topic=command_topic(NODE_ID.root, "lease.renewed"),
            payload=response.model_dump_json().encode("utf-8"),
        ),
        SESSION_ID,
    ) is True
    assert service.current_revision(plan.plan_id, request_payload.lease_id) == 2
    assert service.handle_renewed(
        MqttMessage(
            topic=command_topic(NODE_ID.root, "lease.renewed"),
            payload=response.model_dump_json().encode("utf-8"),
        ),
        SESSION_ID,
    ) is False
