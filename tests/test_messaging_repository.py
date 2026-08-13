"""P3.5：可靠消息与审计仓储测试（inbox/outbox/domain_events/audit_logs）。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from master.domain.enums import OutboxStatus
from master.domain.models import (
    AuditLog,
    DomainEvent,
    InboxMessage,
    OutboxMessage,
)
from master.domain.time import utcnow


def _uow(container):
    """container.uow_factory() 返回工厂单例；再调用一次得到可 with 的 UoW。"""
    return container.uow_factory()()


def _make_inbox(origin: str = "node-1", msg_id: str = "m-1", **kw) -> InboxMessage:
    msg = InboxMessage(
        origin_id=origin,
        message_id=msg_id,
        message_type="run.result",
        payload_hash="abc123",
    )
    for key, value in kw.items():
        setattr(msg, key, value)
    return msg


def _make_outbox(outbox_id: str = "O-1", **kw) -> OutboxMessage:
    msg = OutboxMessage(
        outbox_id=outbox_id,
        aggregate_type="task_run",
        aggregate_id="R-1",
        topic="aetp/v1/master/agents/bench-001/commands/run",
        payload={"cmd": "run"},
        qos=1,
        status=OutboxStatus.PENDING,
    )
    for key, value in kw.items():
        setattr(msg, key, value)
    return msg


def _make_event(event_id: str = "E-1", **kw) -> DomainEvent:
    ev = DomainEvent(
        event_id=event_id,
        project_id="p1",
        event_type="run.created",
        aggregate_id="R-1",
        payload={"run_id": "R-1"},
    )
    for key, value in kw.items():
        setattr(ev, key, value)
    return ev


def _make_audit(audit_id: str = "AUD-1", **kw) -> AuditLog:
    log = AuditLog(
        audit_id=audit_id,
        project_id="p1",
        actor_id=1,
        action="member.add",
        resource_type="project_member",
        resource_id="u-1",
        detail={"before": None, "after": "operator"},
    )
    for key, value in kw.items():
        setattr(log, key, value)
    return log


# ---------- inbox_messages ----------


def test_inbox_add_and_dedup(client):
    """(origin_id, message_id) 幂等：重复投递返回已存在记录，不重复入库。"""
    container = client.app.state.container
    with _uow(container) as uow:
        first = uow.inbox_messages.add(_make_inbox())
        assert first.id is not None
        dup = uow.inbox_messages.add(_make_inbox())
        assert dup.id == first.id  # 同键幂等
    with _uow(container) as uow:
        assert len(uow.inbox_messages.list_unprocessed()) == 1


def test_inbox_mark_processed(client):
    container = client.app.state.container
    with _uow(container) as uow:
        msg = uow.inbox_messages.add(_make_inbox())
        processed = uow.inbox_messages.mark_processed(msg)
        assert processed.processed_at is not None
    with _uow(container) as uow:
        assert len(uow.inbox_messages.list_unprocessed()) == 0


# ---------- outbox_messages ----------


def test_outbox_enqueue_and_get(client):
    container = client.app.state.container
    with _uow(container) as uow:
        created = uow.outbox_messages.enqueue(_make_outbox())
        assert created.id is not None
        fetched = uow.outbox_messages.get_by_outbox_id("O-1")
        assert fetched is not None
        assert fetched.topic == "aetp/v1/master/agents/bench-001/commands/run"
        assert fetched.payload == {"cmd": "run"}
        assert fetched.qos == 1
        assert fetched.status == OutboxStatus.PENDING
        assert fetched.attempts == 0


def test_outbox_claim_due_marks_sending(client):
    """claim_due：到期 pending 被取走并标记 sending，attempts+1。"""
    container = client.app.state.container
    now = utcnow()
    with _uow(container) as uow:
        uow.outbox_messages.enqueue(_make_outbox("O-1"))
        uow.outbox_messages.enqueue(_make_outbox("O-2", next_attempt_at=now))
    with _uow(container) as uow:
        claimed = uow.outbox_messages.claim_due(now=now, limit=10)
        assert {c.outbox_id for c in claimed} == {"O-1", "O-2"}
        assert all(c.status == OutboxStatus.SENDING for c in claimed)
        assert all(c.attempts == 1 for c in claimed)
        assert all(c.sent_at is not None for c in claimed)


def test_outbox_claim_due_skips_future(client):
    """next_attempt_at 在未来（退避中）的不取。"""
    container = client.app.state.container
    now = utcnow()
    from datetime import timedelta

    future = now + timedelta(hours=1)
    with _uow(container) as uow:
        uow.outbox_messages.enqueue(_make_outbox("O-future", next_attempt_at=future))
        uow.outbox_messages.enqueue(_make_outbox("O-now"))
    with _uow(container) as uow:
        claimed = uow.outbox_messages.claim_due(now=now, limit=10)
        assert [c.outbox_id for c in claimed] == ["O-now"]


def test_outbox_claim_due_includes_expired_retrying(client):
    container = client.app.state.container
    now = utcnow()
    from datetime import timedelta

    with _uow(container) as uow:
        uow.outbox_messages.enqueue(
            _make_outbox(
                "O-retry",
                status=OutboxStatus.RETRYING,
                next_attempt_at=now - timedelta(seconds=5),
            )
        )
    with _uow(container) as uow:
        claimed = uow.outbox_messages.claim_due(now=now, limit=10)
        assert [c.outbox_id for c in claimed] == ["O-retry"]
        assert claimed[0].status == OutboxStatus.SENDING


def test_outbox_update_status(client):
    container = client.app.state.container
    with _uow(container) as uow:
        msg = uow.outbox_messages.enqueue(_make_outbox())
        msg.status = OutboxStatus.SUCCEEDED
        msg.attempts = 1
        updated = uow.outbox_messages.update(msg)
        assert updated.status == OutboxStatus.SUCCEEDED
        assert updated.attempts == 1


# ---------- domain_events ----------


def test_domain_event_sequence_monotonic(client):
    """sequence 全局单调递增（事件顺序）。"""
    container = client.app.state.container
    with _uow(container) as uow:
        e1 = uow.domain_events.add(_make_event("E-1"))
        e2 = uow.domain_events.add(_make_event("E-2"))
        e3 = uow.domain_events.add(_make_event("E-3"))
        assert e1.sequence == 1
        assert e2.sequence == 2
        assert e3.sequence == 3


def test_domain_event_list_order_and_filters(client):
    container = client.app.state.container
    with _uow(container) as uow:
        uow.domain_events.add(_make_event("E-1", event_type="run.created"))
        uow.domain_events.add(
            _make_event("E-2", project_id="p2", event_type="run.attempt_failed")
        )
        uow.domain_events.add(_make_event("E-3", event_type="run.succeeded"))
    with _uow(container) as uow:
        all_events = uow.domain_events.list()
        assert [e.sequence for e in all_events] == [1, 2, 3]
        p1 = uow.domain_events.list(project_id="p1")
        assert [e.event_id for e in p1] == ["E-1", "E-3"]
        after = uow.domain_events.list(after_sequence=1)
        assert [e.event_id for e in after] == ["E-2", "E-3"]


def test_domain_event_unique_event_id(client):
    container = client.app.state.container
    with pytest.raises(IntegrityError):
        with _uow(container) as uow:
            uow.domain_events.add(_make_event("E-1"))
            uow.domain_events.add(_make_event("E-1"))


# ---------- audit_logs ----------


def test_audit_add_and_get(client):
    container = client.app.state.container
    with _uow(container) as uow:
        created = uow.audit_logs.add(_make_audit())
        assert created.id is not None
        fetched = uow.audit_logs.get_by_audit_id("AUD-1")
        assert fetched is not None
        assert fetched.actor_id == 1
        assert fetched.action == "member.add"
        assert fetched.resource_id == "u-1"
        assert fetched.detail == {"before": None, "after": "operator"}


def test_audit_list_filters(client):
    container = client.app.state.container
    with _uow(container) as uow:
        uow.audit_logs.add(_make_audit("AUD-1"))
        uow.audit_logs.add(
            _make_audit("AUD-2", actor_id=2, action="member.remove")
        )
        uow.audit_logs.add(
            _make_audit("AUD-3", project_id="p2", action="integration.key_rotate")
        )
    with _uow(container) as uow:
        assert len(uow.audit_logs.list(project_id="p1")) == 2
        assert len(uow.audit_logs.list(actor_id=2)) == 1
        assert len(uow.audit_logs.list(action="member.remove")) == 1
        assert len(uow.audit_logs.list(limit=1, offset=1)) == 1
