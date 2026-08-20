"""P5.4：CommandDispatcher 命令分发测试。

验收要点：
1. run.assign 先 claim 后 ACK（严格顺序）
2. 重复 assign 幂等 ACK（inbox 去重 + claim 幂等）
3. 未注册不接受命令
4. run.cancel 设置取消标志
5. 非法 Envelope/sender/topic 静默忽略
6. run.cancel 重复幂等
7. run.cancel 目标不存在静默忽略
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import RunAssignPayload, RunCancelPayload
from aetp_protocol.topics import command_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.command_dispatcher import CommandDispatcher
from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus
from common.transport import MqttMessage


def _now() -> datetime:
    # 用远未来固定时间：账本用真实 _utcnow() 写 next_attempt_at，
    # claim_due_outbox 必须传一个 >= 它的时间才能取到消息，避免随墙钟漂移。
    return datetime(2099, 1, 1, tzinfo=UTC)


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_host="broker.test",
    mqtt_port=1883,
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
)


def _make_dispatcher(
    tmp_path, *, registered: bool = True
) -> tuple[CommandDispatcher, SQLiteLedger]:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    dispatcher = CommandDispatcher(
        _SETTINGS,
        ledger,
        is_registered=lambda: registered,
        now=_now,
    )
    return dispatcher, ledger


def _run_assign_envelope(
    *,
    run_id: str = "R-1",
    attempt_no: int = 1,
    dispatch_id: str = "D-1",
    task_type: str = "can_test",
    message_id: str | None = None,
    sender_id: str = "aetp-master",
) -> Envelope:
    payload = RunAssignPayload(
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        shard_index=0,
        run_id=run_id,
        attempt_no=attempt_no,
        dispatch_id=dispatch_id,
        task_type=task_type,
        plugin_version="1.0.0",
        script_ref={
            "script_id": "S-1",
            "version": 1,
            "sha256": "a" * 64,
            "download_url": "http://127.0.0.1:8000/api/v1/internal/scripts/S-1/download",
        },
        case_keys=["case-1"],
        timeout_s=1800,
    )
    return Envelope(
        message_id=message_id or uuid.uuid4().hex,
        message_type=MessageType.RUN_ASSIGN.value,
        sent_at=_now(),
        sender=Sender(
            kind=SenderKind.MASTER, id=sender_id, session_id="master-sess"
        ),
        trace_id="bench-001",
        payload=payload.model_dump(mode="json"),
    )


def _run_cancel_envelope(
    *,
    run_id: str = "R-1",
    reason: str = "user requested",
    message_id: str | None = None,
    sender_id: str = "aetp-master",
) -> Envelope:
    payload = RunCancelPayload(run_id=run_id, reason=reason)
    return Envelope(
        message_id=message_id or uuid.uuid4().hex,
        message_type=MessageType.RUN_CANCEL.value,
        sent_at=_now(),
        sender=Sender(
            kind=SenderKind.MASTER, id=sender_id, session_id="master-sess"
        ),
        trace_id="bench-001",
        payload=payload.model_dump(mode="json"),
    )


def _mqtt_message(envelope: Envelope, topic: str) -> MqttMessage:
    return MqttMessage(
        topic=topic,
        payload=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
    )


# -----------------------------------------------------------------------
# run.assign：先 claim 后 ACK
# -----------------------------------------------------------------------

def test_run_assign_claims_and_enqueues_ack(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    env = _run_assign_envelope()
    topic = command_topic("bench-001", "assign")

    result = dispatcher.handle_command(_mqtt_message(env, topic))
    assert result is True

    # Run 已被 claim
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.status is AgentRunStatus.CLAIMED
    assert run.attempt_no == 1

    # ACK 已写入 outbox
    pending = ledger.claim_due_outbox(10, _now())
    assert len(pending) == 1
    ack_env = Envelope.model_validate(pending[0].payload)
    assert ack_env.message_type == MessageType.RUN_ACK.value
    assert ack_env.correlation_id == env.message_id
    assert ack_env.payload["run_id"] == "R-1"
    assert ack_env.payload["accepted"] is True


# -----------------------------------------------------------------------
# 重复 assign 幂等
# -----------------------------------------------------------------------

def test_duplicate_assign_is_idempotent(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    msg_id = uuid.uuid4().hex
    env = _run_assign_envelope(message_id=msg_id)
    topic = command_topic("bench-001", "assign")

    assert dispatcher.handle_command(_mqtt_message(env, topic)) is True
    # 同一 message_id 第二次：inbox 去重，仍回 ACK
    assert dispatcher.handle_command(_mqtt_message(env, topic)) is True

    # Run 只被 claim 一次
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.attempt_no == 1

    # Inbox 只记录一次
    assert ledger.record_inbox("aetp-master", msg_id, "run.assign") is False


def test_duplicate_attempt_with_different_message_id(tmp_path) -> None:
    """同一 (run_id, attempt_no) 不同 message_id：claim 幂等仍回 ACK。"""
    dispatcher, ledger = _make_dispatcher(tmp_path)
    topic = command_topic("bench-001", "assign")

    env1 = _run_assign_envelope(message_id=uuid.uuid4().hex)
    assert dispatcher.handle_command(_mqtt_message(env1, topic)) is True

    env2 = _run_assign_envelope(message_id=uuid.uuid4().hex)
    assert dispatcher.handle_command(_mqtt_message(env2, topic)) is True

    # Run 只被 claim 一次
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.attempt_no == 1


# -----------------------------------------------------------------------
# 未注册不接受命令
# -----------------------------------------------------------------------

def test_assign_rejected_when_not_registered(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path, registered=False)
    env = _run_assign_envelope()
    topic = command_topic("bench-001", "assign")

    result = dispatcher.handle_command(_mqtt_message(env, topic))
    assert result is False
    assert ledger.get_run("R-1") is None


def test_cancel_rejected_when_not_registered(tmp_path) -> None:
    dispatcher, _ledger = _make_dispatcher(tmp_path, registered=False)
    env = _run_cancel_envelope()
    topic = command_topic("bench-001", "cancel")

    result = dispatcher.handle_command(_mqtt_message(env, topic))
    assert result is False


# -----------------------------------------------------------------------
# run.cancel
# -----------------------------------------------------------------------

def test_run_cancel_sets_cancelled_flag(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    topic_assign = command_topic("bench-001", "assign")
    topic_cancel = command_topic("bench-001", "cancel")

    # 先 claim
    assign_env = _run_assign_envelope()
    dispatcher.handle_command(_mqtt_message(assign_env, topic_assign))

    run = ledger.get_run("R-1")
    assert run is not None
    assert run.cancelled is False

    # 取消
    cancel_env = _run_cancel_envelope()
    result = dispatcher.handle_command(_mqtt_message(cancel_env, topic_cancel))
    assert result is True

    run = ledger.get_run("R-1")
    assert run is not None
    assert run.cancelled is True


def test_run_cancel_duplicate_is_idempotent(tmp_path) -> None:
    dispatcher, _ledger = _make_dispatcher(tmp_path)
    topic_assign = command_topic("bench-001", "assign")
    topic_cancel = command_topic("bench-001", "cancel")

    assign_env = _run_assign_envelope()
    dispatcher.handle_command(_mqtt_message(assign_env, topic_assign))

    msg_id = uuid.uuid4().hex
    cancel_env = _run_cancel_envelope(message_id=msg_id)
    assert dispatcher.handle_command(_mqtt_message(cancel_env, topic_cancel)) is True
    # 同一 message_id 第二次：inbox 去重
    assert dispatcher.handle_command(_mqtt_message(cancel_env, topic_cancel)) is True


def test_run_cancel_nonexistent_run_is_silent(tmp_path) -> None:
    dispatcher, _ledger = _make_dispatcher(tmp_path)
    cancel_env = _run_cancel_envelope(run_id="no-such-run")
    topic = command_topic("bench-001", "cancel")
    result = dispatcher.handle_command(_mqtt_message(cancel_env, topic))
    assert result is True  # 静默忽略


def test_run_cancel_already_succeeded_is_silent(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    topic_assign = command_topic("bench-001", "assign")
    topic_cancel = command_topic("bench-001", "cancel")

    assign_env = _run_assign_envelope()
    dispatcher.handle_command(_mqtt_message(assign_env, topic_assign))

    # 手动将 Run 设为 succeeded
    run = ledger.get_run("R-1")
    run.status = AgentRunStatus.SUCCEEDED
    ledger.update_run(run)

    cancel_env = _run_cancel_envelope()
    result = dispatcher.handle_command(_mqtt_message(cancel_env, topic_cancel))
    assert result is True  # 静默忽略

    run = ledger.get_run("R-1")
    assert run.cancelled is False  # 已终态不被修改


# -----------------------------------------------------------------------
# 非法消息
# -----------------------------------------------------------------------

def test_invalid_envelope_rejected(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    topic = command_topic("bench-001", "assign")
    msg = MqttMessage(topic=topic, payload=b"not-json")
    assert dispatcher.handle_command(msg) is False
    assert ledger.get_run("R-1") is None


def test_sender_not_master_rejected(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    env = _run_assign_envelope(sender_id="rogue-agent")
    topic = command_topic("bench-001", "assign")
    assert dispatcher.handle_command(_mqtt_message(env, topic)) is False
    assert ledger.get_run("R-1") is None


def test_topic_mismatch_rejected(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    env = _run_assign_envelope()
    # 错误 topic（events 而非 commands）
    topic = "aetp/v1/agents/bench-001/events/assign"
    assert dispatcher.handle_command(_mqtt_message(env, topic)) is False
    assert ledger.get_run("R-1") is None


def test_node_id_mismatch_rejected(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    env = _run_assign_envelope()
    # 发给其他节点的 topic
    topic = command_topic("other-node", "assign")
    assert dispatcher.handle_command(_mqtt_message(env, topic)) is False
    assert ledger.get_run("R-1") is None


# -----------------------------------------------------------------------
# 多 Run 并行 claim
# -----------------------------------------------------------------------

def test_multiple_runs_claimed_independently(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    topic = command_topic("bench-001", "assign")

    for run_id in ("R-1", "R-2", "R-3"):
        env = _run_assign_envelope(run_id=run_id)
        assert dispatcher.handle_command(_mqtt_message(env, topic)) is True

    assert ledger.get_run("R-1") is not None
    assert ledger.get_run("R-2") is not None
    assert ledger.get_run("R-3") is not None

    # 每个 run 各有一条 ACK outbox
    pending = ledger.claim_due_outbox(10, _now())
    assert len(pending) == 3
