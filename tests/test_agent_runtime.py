"""P5.3+P5.4：AgentRuntime 生命周期与命令路由测试。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

import pytest
from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.topics import command_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.runtime import AgentRuntime
from agent.application.services.registration_service import RegistrationService
from agent.application.services.script_preflight_service import ScriptPreflightService
from agent.config import AgentSettings
from common.transport import MqttMessage


class RuntimeTransport:
    """可驱动连接回调与入站消息的假 Transport。"""

    def __init__(self) -> None:
        self.connected = False
        self.message_handler: Callable[[MqttMessage], Awaitable[None]] | None = None
        self.connection_handler: Callable[[bool, str], Awaitable[None]] | None = None
        self.subscriptions: list[str] = []
        self.published: list[tuple[str, bytes, int]] = []
        self.session_id = "session-1"

    def on_message(self, handler) -> None:
        self.message_handler = handler

    def on_connection_change(self, handler) -> None:
        self.connection_handler = handler

    async def connect(self) -> None:
        self.connected = True
        assert self.connection_handler is not None
        await self.connection_handler(True, self.session_id)

    async def disconnect(self) -> None:
        if self.connected:
            self.connected = False
            assert self.connection_handler is not None
            await self.connection_handler(False, self.session_id)

    async def subscribe(self, topics: list[str]) -> None:
        self.subscriptions = list(topics)

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.published.append((topic, payload, qos))

    async def emit(self, topic: str, payload: bytes) -> None:
        assert self.message_handler is not None
        await self.message_handler(MqttMessage(topic=topic, payload=payload))


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
    registration_timeout_s=1,
    heartbeat_interval_s=60,
)


def _ack(correlation_id: str | None, session_id: str) -> bytes:
    envelope = Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.REGISTER_ACK.value,
        sent_at=datetime.now(UTC),
        sender=Sender(
            kind=SenderKind.MASTER,
            id="aetp-master",
            session_id="master-session",
        ),
        correlation_id=correlation_id,
        trace_id="bench-001",
        payload={
            "node_id": "bench-001",
            "session_id": session_id,
            "accepted": True,
        },
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


@pytest.mark.asyncio
async def test_runtime_connects_registers_then_starts_heartbeat(tmp_path) -> None:
    transport = RuntimeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(transport, ledger, _SETTINGS, session_id="old-session")
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    await runtime.start()
    await asyncio.sleep(0.05)

    assert set(transport.subscriptions) == {
        command_topic("bench-001", "register-ack"),
        command_topic("bench-001", "assign"),
        command_topic("bench-001", "cancel"),
        command_topic("bench-001", "verify"),
        command_topic("bench-001", "parse"),
    }
    assert registration.registered is False
    assert registration.pending_register_message_id is not None
    # 连接成功后已自动写入/发布注册消息，但 ACK 前不发布 heartbeat。
    assert any("/events/register" in topic for topic, _, _ in transport.published)
    assert not any("/events/heartbeat" in topic for topic, _, _ in transport.published)

    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(registration.pending_register_message_id, "session-1"),
    )
    await asyncio.sleep(0.05)
    assert registration.registered is True
    assert any("/events/heartbeat" in topic for topic, _, _ in transport.published)

    await runtime.stop()
    assert registration.registered is False


@pytest.mark.asyncio
async def test_runtime_disconnect_invalidates_registration(tmp_path) -> None:
    transport = RuntimeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(transport, ledger, _SETTINGS)
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    await runtime.start()
    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(registration.pending_register_message_id, "session-1"),
    )
    await asyncio.sleep(0.02)
    assert registration.registered is True

    await transport.disconnect()
    assert registration.registered is False
    await runtime.stop()


def test_runtime_rejects_non_master_script_preflight_sender(tmp_path) -> None:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    transport = RuntimeTransport()
    registration = RegistrationService(transport, ledger, _SETTINGS)
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    class Preflight:
        called = False

        def handle_verify(self, topic, envelope):
            self.called = True
            return True

    preflight = Preflight()
    runtime._script_preflight_obj = cast(ScriptPreflightService, preflight)
    envelope = Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.SCRIPT_VERIFY.value,
        sent_at=datetime.now(UTC),
        sender=Sender(
            kind=SenderKind.AGENT,
            id="bench-001",
            session_id="agent-session",
        ),
        trace_id="trace-1",
        payload={},
    )

    assert runtime._route_script_preflight(
        MqttMessage(
            topic=command_topic("bench-001", "verify"),
            payload=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
        )
    ) is False
    assert preflight.called is False


@pytest.mark.asyncio
async def test_runtime_reconnect_uses_new_session_and_registers_again(tmp_path) -> None:
    transport = RuntimeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(transport, ledger, _SETTINGS)
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    await runtime.start()
    first_message_id = registration.pending_register_message_id
    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(first_message_id, "session-1"),
    )
    await asyncio.sleep(0.02)
    assert registration.registered is True

    await transport.disconnect()
    transport.session_id = "session-2"
    await transport.connect()
    await asyncio.sleep(0.02)

    assert registration.registered is False
    assert registration.session_id == "session-2"
    assert registration.pending_register_message_id != first_message_id

    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(registration.pending_register_message_id, "session-2"),
    )
    await asyncio.sleep(0.02)
    assert registration.registered is True
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_handles_run_assign_after_registration(tmp_path) -> None:
    """P5.4：注册成功后，run.assign 被路由到 CommandDispatcher 并写 ACK outbox。"""
    transport = RuntimeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(transport, ledger, _SETTINGS, session_id="session-1")
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    await runtime.start()
    await asyncio.sleep(0.05)

    # 完成注册
    await transport.emit(
        command_topic("bench-001", "register-ack"),
        _ack(registration.pending_register_message_id, "session-1"),
    )
    await asyncio.sleep(0.05)
    assert registration.registered is True

    # 构造 run.assign 消息
    from aetp_protocol.payloads import RunAssignPayload

    assign_payload = RunAssignPayload(
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        shard_index=0,
        run_id="R-100",
        attempt_no=1,
        dispatch_id="D-100",
        task_type="can_test",
        plugin_version="1.0.0",
        script_ref={
            "script_id": "S-1",
            "version": 1,
            "sha256": "a" * 64,
            "download_url": "http://127.0.0.1:8000/scripts/S-1",
        },
        case_keys=["case-1"],
        timeout_s=600,
    )
    assign_envelope = Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.RUN_ASSIGN.value,
        sent_at=datetime.now(UTC),
        sender=Sender(
            kind=SenderKind.MASTER,
            id="aetp-master",
            session_id="master-sess",
        ),
        trace_id="bench-001",
        payload=assign_payload.model_dump(mode="json"),
    )
    assign_bytes = json.dumps(assign_envelope.model_dump(mode="json")).encode("utf-8")
    await transport.emit(command_topic("bench-001", "assign"), assign_bytes)
    await asyncio.sleep(0.5)

    # Run 已被 claim
    run = ledger.get_run("R-100")
    assert run is not None
    assert run.attempt_no == 1

    # ACK outbox 已写入（通过 outbox loop 发布）
    ack_topics = [t for t, _, _ in transport.published if "/events/ack" in t]
    assert len(ack_topics) >= 1

    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_rejects_assign_before_registration(tmp_path) -> None:
    """P5.4：未注册时 run.assign 被忽略。"""
    transport = RuntimeTransport()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registration = RegistrationService(transport, ledger, _SETTINGS)
    runtime = AgentRuntime(_SETTINGS, transport, ledger, registration)

    await runtime.start()
    await asyncio.sleep(0.05)

    # 未注册状态下发送 run.assign
    from aetp_protocol.payloads import RunAssignPayload

    assign_payload = RunAssignPayload(
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        shard_index=0,
        run_id="R-200",
        attempt_no=1,
        dispatch_id="D-200",
        task_type="can_test",
        plugin_version="1.0.0",
        script_ref={
            "script_id": "S-1",
            "version": 1,
            "sha256": "a" * 64,
            "download_url": "http://127.0.0.1:8000/scripts/S-1",
        },
    )
    assign_envelope = Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.RUN_ASSIGN.value,
        sent_at=datetime.now(UTC),
        sender=Sender(
            kind=SenderKind.MASTER,
            id="aetp-master",
            session_id="master-sess",
        ),
        trace_id="bench-001",
        payload=assign_payload.model_dump(mode="json"),
    )
    assign_bytes = json.dumps(assign_envelope.model_dump(mode="json")).encode("utf-8")
    await transport.emit(command_topic("bench-001", "assign"), assign_bytes)
    await asyncio.sleep(0.05)

    # Run 未被 claim
    assert ledger.get_run("R-200") is None

    await runtime.stop()
