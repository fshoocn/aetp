"""P5.5：Agent 插件包下载、校验、解包和入口点安装测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import zipfile

import pytest

from aetp_protocol.payloads import PluginPackageRef
from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.topics import command_topic

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.command_dispatcher import CommandDispatcher
from agent.config import AgentSettings
from agent.plugins.errors import PluginInstallError
from agent.plugins.execution import AgentPluginRegistry
from agent.plugins.installer import LocalPluginInstaller
from common.transport import MqttMessage


class CompatiblePlugin:
    task_type = "compatible_task"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = "Compatible Task"


def _package_bytes(module_name: str = "remote_plugin") -> bytes:
    source = """\
class RemotePlugin:
    task_type = 'remote_can'
    plugin_version = '2.0.0'
    supported_versions = frozenset({'2.0.0'})
    display_name = 'Remote CAN'

    async def execute(self, context):
        return {'status': 'passed'}

    async def cancel(self):
        return None

    async def analyze_results(self, execution_result, context):
        return execution_result

    async def collect_logs(self, context):
        return None
"""
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(f"{module_name}.py", source)
    return stream.getvalue()


def _ref(data: bytes, *, entry_point: str = "remote_plugin:RemotePlugin"):
    return PluginPackageRef(
        task_type="remote_can",
        package_name="aetp-plugin-remote-can",
        version="2.0.0",
        download_url="https://master.example/plugins/remote-can.whl",
        sha256=hashlib.sha256(data).hexdigest(),
        entry_point=entry_point,
    )


def test_compatible_plugin_skips_installer(tmp_path) -> None:
    registry = AgentPluginRegistry()
    registry.register_installed(CompatiblePlugin())

    class FailingInstaller:
        def install(self, package_ref):
            raise AssertionError("兼容插件不应下载安装")

    plugin = registry.ensure_compatible(
        "compatible_task",
        "1.0.0",
        package_ref=None,
        installer=FailingInstaller(),
    )
    assert plugin.task_type == "compatible_task"


def test_missing_plugin_downloads_verifies_and_loads(tmp_path) -> None:
    data = _package_bytes()
    ref = _ref(data)
    installer = LocalPluginInstaller(
        tmp_path / "plugins", fetcher=lambda url: data
    )
    registry = AgentPluginRegistry()

    plugin = registry.ensure_compatible(
        "remote_can", "2.0.0", package_ref=ref, installer=installer
    )

    assert plugin.task_type == "remote_can"
    assert registry.supported_task_types() == ["remote_can"]
    assert (tmp_path / "plugins" / "remote_can" / "2.0.0" / "aetp-plugin.json").is_file()

    restored = AgentPluginRegistry()
    assert installer.restore(restored) == 1
    assert restored.require_compatible("remote_can", "2.0.0").task_type == "remote_can"


def test_plugin_sha256_mismatch_rejected(tmp_path) -> None:
    data = _package_bytes()
    ref = _ref(data).model_copy(update={"sha256": "0" * 64})
    installer = LocalPluginInstaller(
        tmp_path / "plugins", fetcher=lambda url: data
    )

    with pytest.raises(PluginInstallError, match="SHA-256"):
        installer.install(ref)


def test_plugin_archive_path_traversal_rejected(tmp_path) -> None:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../escape.py", "class Bad: pass")
    data = stream.getvalue()
    ref = _ref(data)
    installer = LocalPluginInstaller(
        tmp_path / "plugins", fetcher=lambda url: data
    )

    with pytest.raises(PluginInstallError, match="非法路径"):
        installer.install(ref)


def test_assign_installs_missing_plugin_before_claim(tmp_path) -> None:
    data = _package_bytes()
    ref = _ref(data)
    settings = AgentSettings(
        node_id="bench-001",
        name="bench",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-001",
        mqtt_use_tls=False,
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentPluginRegistry()
    installer = LocalPluginInstaller(
        tmp_path / "plugins", fetcher=lambda url: data
    )
    dispatcher = CommandDispatcher(
        settings,
        ledger,
        is_registered=lambda: True,
        plugin_registry=registry,
        plugin_installer=installer,
    )
    from aetp_protocol.payloads import RunAssignPayload

    payload = RunAssignPayload(
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        shard_index=0,
        run_id="R-install",
        attempt_no=1,
        dispatch_id="D-1",
        task_type="remote_can",
        plugin_version="2.0.0",
        plugin_ref=ref,
        script_ref={"script_id": "S-1", "version": 1, "sha256": "a" * 64},
    )
    envelope = Envelope(
        message_id="assign-install-1",
        message_type=MessageType.RUN_ASSIGN.value,
        sent_at="2026-08-17T12:00:00Z",
        sender=Sender(
            kind=SenderKind.MASTER,
            id="aetp-master",
            session_id="master-session",
        ),
        trace_id="R-install",
        payload=payload.model_dump(mode="json"),
    )
    message = MqttMessage(
        topic=command_topic("bench-001", "assign"),
        payload=envelope.model_dump_json().encode("utf-8"),
    )

    assert dispatcher.handle_command(message) is True
    assert registry.get("remote_can") is not None
    assert ledger.get_run("R-install") is not None


def test_install_failure_can_retry_same_assign_and_replace_rejected_ack(tmp_path) -> None:
    data = _package_bytes()
    ref = _ref(data)
    state = {"available": False}

    def fetcher(url: str) -> bytes:
        if not state["available"]:
            raise PluginInstallError("暂时不可用")
        return data

    settings = AgentSettings(
        node_id="bench-001",
        name="bench",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-001",
        mqtt_use_tls=False,
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    registry = AgentPluginRegistry()
    installer = LocalPluginInstaller(tmp_path / "plugins", fetcher=fetcher)
    dispatcher = CommandDispatcher(
        settings,
        ledger,
        is_registered=lambda: True,
        plugin_registry=registry,
        plugin_installer=installer,
    )
    from aetp_protocol.payloads import RunAssignPayload

    payload = RunAssignPayload(
        project_id="p1",
        task_id="T-1",
        shard_id="SH-1",
        shard_index=0,
        run_id="R-retry-install",
        attempt_no=1,
        dispatch_id="D-1",
        task_type="remote_can",
        plugin_version="2.0.0",
        plugin_ref=ref,
        script_ref={"script_id": "S-1", "version": 1, "sha256": "a" * 64},
    )
    envelope = Envelope(
        message_id="assign-retry-install-1",
        message_type=MessageType.RUN_ASSIGN.value,
        sent_at="2026-08-17T12:00:00Z",
        sender=Sender(
            kind=SenderKind.MASTER,
            id="aetp-master",
            session_id="master-session",
        ),
        trace_id="R-retry-install",
        payload=payload.model_dump(mode="json"),
    )
    message = MqttMessage(
        topic=command_topic("bench-001", "assign"),
        payload=envelope.model_dump_json().encode("utf-8"),
    )

    assert dispatcher.handle_command(message) is True
    rejected = ledger.claim_due_outbox(10, datetime.now(timezone.utc).replace(tzinfo=None))
    assert rejected[0].payload["payload"]["accepted"] is False
    assert ledger.get_run("R-retry-install") is None

    state["available"] = True
    assert dispatcher.handle_command(message) is True
    accepted = ledger.claim_due_outbox(10, datetime.now(timezone.utc).replace(tzinfo=None))
    assert accepted[0].payload["payload"]["accepted"] is True
    assert ledger.get_run("R-retry-install") is not None


def test_new_package_can_support_an_older_task_plugin_version() -> None:
    class CompatibleV2:
        task_type = "remote_can"
        plugin_version = "2.0.0"
        supported_versions = frozenset({"1.0.0", "2.0.0"})
        display_name = "Remote CAN"

    class Installer:
        def install(self, package_ref):
            return CompatibleV2()

    data = _package_bytes()
    package_ref = _ref(data).model_copy(update={"version": "2.0.0"})
    registry = AgentPluginRegistry()

    plugin = registry.ensure_compatible(
        "remote_can",
        "1.0.0",
        package_ref=package_ref,
        installer=Installer(),
    )

    assert plugin.plugin_version == "2.0.0"
