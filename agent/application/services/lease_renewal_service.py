"""Agent  Lease 续租请求和回执处理。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aetp_protocol.envelope import parse_message
from aetp_protocol.execution import ExecutionPlan
from aetp_protocol.ids import BusinessId, MessageId, SessionId, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import LeaseRenewed, LeaseRenewRequest
from aetp_protocol.topics import (
    parse_topic,
    validate_message_type_for_topic,
    validate_sender_for_topic,
)

from agent.application.services.capability_publisher import CapabilityPublisher
from agent.domain.ledger import Ledger
from common.transport import MqttMessage


@dataclass
class _LeaseState:
    plan: ExecutionPlan
    revision: int
    expires_at: datetime
    pending_message_id: MessageId | None = None


class LeaseRenewalService:
    """维护已接受 Plan 的 Lease revision，并按 TTL 发送续租请求。"""

    def __init__(
        self,
        node_id: BusinessId,
        ledger: Ledger,
        publisher: CapabilityPublisher,
        *,
        master_id: str = "aetp-master",
        renewal_lead_s: int = 15,
        extension_s: int = 60,
        now=None,
    ) -> None:
        if renewal_lead_s < 0 or extension_s <= 0:
            raise ValueError("Lease 续租参数不合法")
        self._node_id = node_id
        self._ledger = ledger
        self._publisher = publisher
        self._master_id = master_id
        self._renewal_lead = timedelta(seconds=renewal_lead_s)
        self._extension = timedelta(seconds=extension_s)
        self._now = now or (lambda: datetime.now(UTC))
        self._leases: dict[tuple[str, str], _LeaseState] = {}
        self._pending: dict[str, tuple[str, str, int]] = {}

    def register_plan(self, plan: ExecutionPlan) -> None:
        """登记 Agent 已接受 Plan 的 Lease 初始状态。"""
        if plan.node_id != self._node_id:
            return
        for binding in plan.resource_bindings:
            key = (plan.plan_id.root, binding.lease_id.root)
            current = self._leases.get(key)
            if current is None or current.revision <= binding.lease_revision:
                self._leases[key] = _LeaseState(
                    plan=plan,
                    revision=binding.lease_revision,
                    expires_at=binding.expires_at,
                )

    def reset_session(self) -> None:
        """切换 session 时清除旧 Plan 和待处理请求。"""
        self._leases.clear()
        self._pending.clear()

    async def run_once(self, session_id: SessionId) -> int:
        """为进入续租窗口的 Lease 写入一次 outbox 请求。"""
        now = self._now()
        created = 0
        for key, state in tuple(self._leases.items()):
            if state.plan.target_session_id != session_id:
                self._leases.pop(key, None)
                continue
            if state.pending_message_id is not None or state.expires_at - now > self._renewal_lead:
                continue
            if state.expires_at <= now:
                continue
            requested_expires_at = min(state.plan.deadline_at, now + self._extension)
            if requested_expires_at <= state.expires_at:
                continue
            request = LeaseRenewRequest(
                plan_id=state.plan.plan_id,
                attempt_id=state.plan.attempt_id,
                lease_id=BusinessId(key[1]),
                revision=state.revision,
                requested_expires_at=requested_expires_at,
            )
            message_id = self._publisher.enqueue_lease_renew(self._ledger, request, session_id)
            state.pending_message_id = message_id
            self._pending[message_id.root] = (key[0], key[1], state.revision)
            created += 1
        return created

    def handle_renewed(self, message: MqttMessage, session_id: SessionId) -> bool:
        """处理 Master 的 lease.renewed 命令并更新当前 revision。"""
        try:
            topic = parse_topic(message.topic)
            if (
                topic.direction != "commands"
                or topic.node_id != self._node_id.root
                or topic.segment != "lease.renewed"
            ):
                return False
            envelope, payload = parse_message(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_topic(message.topic, envelope.sender)
            validate_message_type_for_topic(
                message.topic,
                MessageType(envelope.message_type),
            )
            if envelope.sender.id != stable_id(self._master_id):
                return False
            if not isinstance(payload, LeaseRenewed) or envelope.correlation_id is None:
                return False
            pending = self._pending.pop(envelope.correlation_id.root, None)
            if pending is None:
                return False
            key = (pending[0], pending[1])
            state = self._leases.get(key)
            if state is None or state.plan.target_session_id != session_id:
                return False
            if payload.plan_id != state.plan.plan_id or payload.attempt_id != state.plan.attempt_id:
                return False
            if payload.lease_id.root != key[1] or pending[2] != state.revision:
                return False
            state.pending_message_id = None
            if payload.accepted:
                if payload.expires_at is None or payload.revision <= state.revision:
                    return False
                state.revision = payload.revision
                state.expires_at = payload.expires_at
            return True
        except Exception:
            return False

    def current_revision(self, plan_id: BusinessId, lease_id: BusinessId) -> int | None:
        state = self._leases.get((plan_id.root, lease_id.root))
        return state.revision if state is not None else None


__all__ = ["LeaseRenewalService"]
