"""P5.5：Agent 执行插件 registry 与版本校验测试。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import RunAssignPayload
from aetp_protocol.plugin import PluginMetadata, PluginPackage
from aetp_protocol.topics import command_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.command_dispatcher import CommandDispatcher
from agent.application.services.registration_service import RegistrationService
from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus
from agent.plugins import (
    AgentTaskContext,
    AgentPluginRegistry,
    PluginNotFoundError,
    PluginVersionMismatchError,
)
from common.transport import MqttMessage


def _now() -> datetime:
    return datetime(2099, 1, 1, tzinfo=timezone.utc)


_SETTINGS = AgentSettings(
    node_id="bench-001",
    name="bench",
    master_id="aetp-master",
    mqtt_host="broker.test",
    mqtt_port=1883,
    mqtt_client_id="aetp-agent-bench-001",
    mqtt_use_tls=False,
)


class FakeExecutionPlugin:
    """测试替身：只实现 Agent 侧执行职责。"""

    task_type = "fake_task"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = "Fake Task"

    async def execute(self, context: AgentTaskContext):
        await context.progress(100, "completed", "done")
        await context.log("info", "fake execution")
        return {"status": "passed"}

    async def cancel(self) -> None:
        return None

    async def analyze_results(self, execution_result, context):
        return {"passed": execution_result["status"] == "passed"}

    async def collect_logs(self, context) -> None:
        return None


# -----------------------------------------------------------------------
# Agent Plugin Registry
# -----------------------------------------------------------------------


def test_plugin_registry_register_and_get() -> None:
    registry = AgentPluginRegistry()
    plugin = FakeExecutionPlugin()
    registry.register_installed(plugin)

    assert registry.get("fake_task") is plugin
    assert registry.get("nonexistent") is None


def test_plugin_registry_capabilities() -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(FakeExecutionPlugin())

    caps = registry.capabilities()
    assert len(caps) == 1
    assert caps[0].task_type == "fake_task"
    assert caps[0].plugin_version == "1.0.0"
    assert caps[0].supports("1.0.0") is True
    assert caps[0].supports("2.0.0") is False


def test_plugin_registry_supported_task_types() -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(FakeExecutionPlugin())
    assert registry.supported_task_types() == ["fake_task"]


def test_plugin_registry_require_compatible_ok() -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(FakeExecutionPlugin())
    assert registry.require_compatible("fake_task", "1.0.0").task_type == "fake_task"


def test_plugin_registry_require_compatible_version_mismatch() -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(FakeExecutionPlugin())
    with pytest.raises(PluginVersionMismatchError):
        registry.require_compatible("fake_task", "2.0.0")


def test_plugin_registry_require_not_found() -> None:
    registry = AgentPluginRegistry()
    with pytest.raises(PluginNotFoundError):
        registry.require("nonexistent")


def test_plugin_registry_revision_increments() -> None:
    registry = AgentPluginRegistry()
    assert registry.revision == 0
    registry.register_installed(FakeExecutionPlugin())
    assert registry.revision == 1


def test_agent_registry_discovers_shared_package(monkeypatch) -> None:
    package = PluginPackage(
        metadata=PluginMetadata(
            task_type="fake_task",
            plugin_version="1.0.0",
            supported_versions=frozenset({"1.0.0"}),
        ),
        master=object(),
        agent=FakeExecutionPlugin(),
    )
    entry_point = SimpleNamespace(
        name="fake_task",
        value="tests.test_agent_plugins:shared_package",
        load=lambda: package,
    )
    monkeypatch.setattr(
        "agent.plugins.execution.entry_points",
        lambda group=None: [entry_point] if group == "aetp.plugins" else [],
    )

    registry = AgentPluginRegistry()
    assert registry.discover("aetp.plugins") == 1
    assert registry.require("fake_task").task_type == "fake_task"


# -----------------------------------------------------------------------
# Registration: plugin_versions from registry
# -----------------------------------------------------------------------


class FakeTransport:
    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        return None


def test_registration_payload_includes_plugin_versions(tmp_path) -> None:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentPluginRegistry()
    registry.register_installed(FakeExecutionPlugin())
    service = RegistrationService(
        FakeTransport(),
        ledger,
        _SETTINGS,
        session_id="sess-1",
        plugin_registry=registry,
        now=_now,
    )

    payload = service.build_register_payload()
    assert payload.plugin_versions == {"fake_task": "1.0.0"}
    assert payload.supported_versions == {"fake_task": ["1.0.0"]}


def test_registration_payload_empty_without_registry(tmp_path) -> None:
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    service = RegistrationService(
        FakeTransport(), ledger, _SETTINGS, session_id="sess-1", now=_now
    )
    payload = service.build_register_payload()
    assert payload.plugin_versions == {}
    assert payload.supported_versions == {}


# -----------------------------------------------------------------------
# CommandDispatcher: version check
# -----------------------------------------------------------------------


def _make_dispatcher(tmp_path, *, plugin_registry=None):
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    dispatcher = CommandDispatcher(
        _SETTINGS,
        ledger,
        is_registered=lambda: True,
        plugin_registry=plugin_registry,
        now=_now,
    )
    return dispatcher, ledger


def _run_assign_envelope(
    *, task_type: str = "fake_task", plugin_version: str = "1.0.0"
) -> Envelope:
    payload = RunAssignPayload(
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        shard_index=0,
        run_id="R-1",
        attempt_no=1,
        dispatch_id="D-1",
        task_type=task_type,
        plugin_version=plugin_version,
        script_ref={"script_id": "S-1", "version": 1, "sha256": "a" * 64},
        case_keys=["case-1"],
        timeout_s=600,
    )
    return Envelope(
        message_id=uuid.uuid4().hex,
        message_type=MessageType.RUN_ASSIGN.value,
        sent_at=_now(),
        sender=Sender(
            kind=SenderKind.MASTER,
            id="aetp-master",
            session_id="master-sess",
        ),
        trace_id="bench-001",
        payload=payload.model_dump(mode="json"),
    )


def _mqtt(envelope: Envelope) -> MqttMessage:
    return MqttMessage(
        topic=command_topic("bench-001", "assign"),
        payload=json.dumps(envelope.model_dump(mode="json")).encode("utf-8"),
    )


def test_assign_accepted_with_compatible_version(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(FakeExecutionPlugin())
    dispatcher, ledger = _make_dispatcher(tmp_path, plugin_registry=registry)

    assert dispatcher.handle_command(_mqtt(_run_assign_envelope())) is True
    run = ledger.get_run("R-1")
    assert run is not None
    assert run.status is AgentRunStatus.CLAIMED


def test_assign_rejected_with_version_mismatch(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(FakeExecutionPlugin())
    dispatcher, ledger = _make_dispatcher(tmp_path, plugin_registry=registry)

    assert dispatcher.handle_command(
        _mqtt(_run_assign_envelope(plugin_version="99.0.0"))
    ) is True
    assert ledger.get_run("R-1") is None
    pending = ledger.claim_due_outbox(10, _now())
    assert len(pending) == 1
    assert Envelope.model_validate(pending[0].payload).payload["accepted"] is False


def test_assign_rejected_with_plugin_not_found(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(
        tmp_path, plugin_registry=AgentPluginRegistry()
    )

    assert dispatcher.handle_command(
        _mqtt(_run_assign_envelope(task_type="missing_task"))
    ) is True
    assert ledger.get_run("R-1") is None


def test_assign_skips_version_check_without_registry(tmp_path) -> None:
    dispatcher, ledger = _make_dispatcher(tmp_path)
    assert dispatcher.handle_command(
        _mqtt(_run_assign_envelope(task_type="anything", plugin_version="99.0.0"))
    ) is True
    assert ledger.get_run("R-1") is not None
