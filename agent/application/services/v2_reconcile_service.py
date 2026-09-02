"""Agent V2 重连对账发送与响应处理。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from aetp_protocol.execution import CaseResult, ExecutionResult, ExecutionStatus
from aetp_protocol.ids import BusinessId, MessageId, SessionId, Sha256, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionReconcile, ExecutionReconcileResult, ReconcileAttempt
from aetp_protocol.topics import parse_v2_topic, validate_message_type_for_v2_topic, validate_sender_for_v2_topic
from aetp_protocol.v2_envelope import parse_v2_message

from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.domain.enums import AgentRunStatus
from agent.domain.ledger import AgentRun, Ledger
from common.transport import MqttMessage

ReconcileState = Literal["running", "succeeded", "failed", "cancelled", "timed_out"]


class AgentV2ReconcileService:
    """根据本地账本发布对账，不在本地决定 Master 状态。"""

    def __init__(
        self,
        node_id: BusinessId,
        ledger: Ledger,
        publisher: AgentV2CapabilityPublisher,
        *,
        master_id: str = "aetp-master",
    ) -> None:
        self._node_id = node_id
        self._ledger = ledger
        self._publisher = publisher
        self._master_id = master_id
        self._pending_message_id: MessageId | None = None

    def reset_session(self) -> None:
        """切换 Agent session 时丢弃旧对账响应关联。"""
        self._pending_message_id = None

    def enqueue(self, session_id: SessionId) -> str:
        """把本地所有带 Plan 身份的记录作为一次对账事件入队。"""
        attempts = tuple(
            attempt
            for run in self._ledger.list_reconcile_runs()
            if (attempt := self._to_attempt(run)) is not None
        )
        reconcile = ExecutionReconcile(node_id=self._node_id, attempts=attempts)
        outbox_id = self._publisher.enqueue_execution_reconcile(
            self._ledger,
            reconcile,
            session_id,
        )
        entry = self._ledger.get_outbox(outbox_id)
        if entry is None:
            raise RuntimeError("对账事件未写入本地 outbox")
        self._pending_message_id = MessageId(str(entry.payload["message_id"]))
        return outbox_id

    def handle_result(self, message: MqttMessage, session_id: SessionId) -> bool:
        """接收 Master 对账响应；响应只确认，不直接改变本地终态。"""
        try:
            topic = parse_v2_topic(message.topic)
            if (
                topic.direction != "commands"
                or topic.node_id != self._node_id.root
                or topic.segment != "execution.reconcile_result"
            ):
                return False
            envelope, payload = parse_v2_message(json.loads(message.payload.decode("utf-8")))
            validate_sender_for_v2_topic(message.topic, envelope.sender)
            validate_message_type_for_v2_topic(message.topic, MessageType(envelope.message_type))
            if envelope.sender.id != stable_id(self._master_id):
                return False
            if envelope.correlation_id is None or envelope.correlation_id != self._pending_message_id:
                return False
            if not isinstance(payload, ExecutionReconcileResult) or payload.node_id != self._node_id:
                return False
            del session_id
            self._pending_message_id = None
            return True
        except Exception:
            return False

    @staticmethod
    def _to_attempt(run: AgentRun) -> ReconcileAttempt | None:
        if run.plan_id is None or run.attempt_id is None or run.shard_id is None or run.plan_hash is None:
            return None
        states: dict[AgentRunStatus, ReconcileState] = {
            AgentRunStatus.CLAIMED: "running",
            AgentRunStatus.RUNNING: "running",
            AgentRunStatus.SUCCEEDED: "succeeded",
            AgentRunStatus.FAILED: "failed",
            AgentRunStatus.CANCELLED: "cancelled",
            AgentRunStatus.TIMED_OUT: "timed_out",
        }
        state = states[run.status]
        result = _result_for_run(run) if state != "running" else None
        return ReconcileAttempt(
            attempt_id=BusinessId(run.attempt_id),
            plan_id=BusinessId(run.plan_id),
            plan_hash=Sha256(run.plan_hash),
            state=state,
            last_progress_sequence=run.last_progress_sequence,
            result=result,
        )

def _result_for_run(run: AgentRun) -> ExecutionResult:
    status = {
        AgentRunStatus.SUCCEEDED: ExecutionStatus.SUCCEEDED,
        AgentRunStatus.FAILED: ExecutionStatus.FAILED,
        AgentRunStatus.CANCELLED: ExecutionStatus.CANCELLED,
        AgentRunStatus.TIMED_OUT: ExecutionStatus.TIMED_OUT,
    }[run.status]
    summary = run.result_summary
    raw_cases = summary.get("case_results", ())
    cases = tuple(
        CaseResult.model_validate(item)
        for item in raw_cases
        if isinstance(item, Mapping)
    )
    return ExecutionResult(
        status=status,
        passed=status is ExecutionStatus.SUCCEEDED and summary.get("passed", True) is not False,
        case_results=cases,
        metrics=dict(summary.get("metrics") or {}),
        data=dict(summary.get("data") or {}),
    )


__all__ = ["AgentV2ReconcileService"]
