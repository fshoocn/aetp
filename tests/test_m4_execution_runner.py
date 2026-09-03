"""M4  execution runner 基础闭环测试。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aetp_protocol.envelope import parse_message
from aetp_protocol.execution import ExecutionStatus
from aetp_protocol.ids import MessageId, PluginId, SemVer, Sha256
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import ExecutionFinished
from aetp_protocol.plan_hash import with_plan_hash
from aetp_protocol.plugin_types import EntrypointRef, PluginPoint, PluginRef
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.application.services.execution_runner import ExecutionRunner
from agent.application.services.execution_service import ExecutionService
from agent.application.services.executor_resolver import ExecutorResolver
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.application.services.script_cache_service import ScriptCacheService
from agent.config import AgentSettings
from agent.plugins.installer import InstalledPlugin
from agent.plugins.registry import PluginRegistry
from tests.test_agent_capability_publisher import FakeTransport
from tests.test_m3_plan_lease import NOW, SESSION_ID, _plan


class FakeExecutor:
    task_type = "org.pytest.executor"
    plugin_version = "2.0.0"
    supported_versions = frozenset({"2.0.0"})
    display_name = "Fake  Executor"
    verify_location = "master"
    parse_location = "master"

    def __init__(self) -> None:
        self.calls = 0
        self.script_path: Path | None = None

    async def execute(self, context) -> dict:
        self.calls += 1
        assert context.run_id
        assert context.case_keys
        raw_path = context.script_ref.get("path")
        if raw_path is not None:
            self.script_path = Path(str(raw_path))
            assert self.script_path.is_dir()
            assert (self.script_path / "test_sample.py").is_file()
        return {"passed": True, "metrics": {"total": 1}, "data": {"source": "fake"}}

    async def cancel(self) -> None:
        return None


class _TestResourceProvider:
    provider_id = "org.test.resource"
    resource_type = "can"

    def discover(self):
        return ()

    async def activate(self, binding) -> None:
        del binding

    async def deactivate(self, binding) -> None:
        del binding


async def _run_once(tmp_path) -> tuple[FakeExecutor, SQLiteLedger]:
    transport = FakeTransport()
    settings = AgentSettings(
        node_id="01J00000000000000000000000",
        name="Bench 01",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-01",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    publisher = CapabilityPublisher(
        transport,
        settings,
        PluginRegistry(),
        capability_scanner=lambda: None,
    )
    execution = ExecutionService(settings, ledger)
    executor = FakeExecutor()
    runner = ExecutionRunner(
        settings,
        ledger,
        execution,
        publisher,
        lambda _plan: executor,
        resource_providers=ResourceProviderRegistry((_TestResourceProvider(),)),
        now=lambda: NOW,
    )
    plan = with_plan_hash(_plan().model_copy(update={"created_at": NOW - timedelta(seconds=1)}))
    ledger.claim_run(plan.run_id.root, plan.attempt_no, plan_id=plan.plan_id.root)
    task = runner.start(
        plan,
        SESSION_ID,
        correlation_id=MessageId("execution-plan-0001"),
    )
    assert task is not None
    await task
    duplicate = runner.start(plan, SESSION_ID, correlation_id=MessageId("execution-plan-0002"))
    assert duplicate is None
    return executor, ledger


def test_execution_runner_executes_once_and_publishes_finished(tmp_path) -> None:
    executor, ledger = asyncio.run(_run_once(tmp_path))

    assert executor.calls == 1
    outbox_entries = ledger.claim_due_outbox(
        10,
        (datetime.now(UTC) + timedelta(seconds=1)).replace(tzinfo=None),
    )
    finished_messages = [item for item in outbox_entries if item.topic.endswith("/execution.finished")]
    assert len(finished_messages) == 1
    envelope, payload = parse_message(finished_messages[0].payload)
    assert envelope.message_type == MessageType.EXECUTION_FINISHED.value
    assert isinstance(payload, ExecutionFinished)
    assert payload.result.status is ExecutionStatus.SUCCEEDED
    assert payload.result.passed is True
    assert ledger.get_run(payload.run_id.root).status.value == "succeeded"


def test_executor_resolver_loads_exact_manifest_entrypoint(tmp_path) -> None:
    root = tmp_path / "plugins" / "org.pytest.executor" / "2.0.0"
    (root / "agent").mkdir(parents=True)
    manifest = PluginManifest(
        schema_version=2,
        id=PluginId("org.pytest.executor"),
        version=SemVer("2.0.0"),
        api_version=SemVer("2.0.0"),
        point=PluginPoint.EXECUTOR,
        display_name="Resolver Executor",
        entrypoints=PluginEntrypoints(
            master=EntrypointRef("plugin:create_plugin"),
            agent=EntrypointRef("plugin:create_plugin"),
        ),
    )
    (root / "plugin.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    (root / "agent" / "plugin.py").write_text(
        "class Executor:\n"
        "    plugin_version = '2.0.0'\n"
        "    async def execute(self, context):\n"
        "        return {}\n"
        "\n"
        "def create_plugin():\n"
        "    return Executor()\n",
        encoding="utf-8",
    )
    registry = PluginRegistry()
    registry.register(
        InstalledPlugin(
            PluginRef(
                plugin_id=manifest.id,
                version=manifest.version,
                archive_sha256=Sha256("a" * 64),
            ),
            root / "plugin.json",
            root,
        )
    )

    resolver = ExecutorResolver(registry)
    executor = resolver.resolve(with_plan_hash(_plan()))

    assert executor.plugin_version == "2.0.0"
    assert resolver.resolve(with_plan_hash(_plan())) is executor


def test_execution_runner_unpacks_cached_script_and_cleans_workspace(tmp_path) -> None:
    script_buffer = io.BytesIO()
    with zipfile.ZipFile(script_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("test_sample.py", "def test_sample():\n    assert True\n")
    script_content = script_buffer.getvalue()
    script_sha256 = hashlib.sha256(script_content).hexdigest()
    transport = FakeTransport()
    settings = AgentSettings(
        node_id="01J00000000000000000000000",
        name="Bench 01",
        master_id="aetp-master",
        mqtt_client_id="aetp-agent-bench-01",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
        script_cache_dir=tmp_path / "scripts",
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    publisher = CapabilityPublisher(transport, settings, PluginRegistry())
    execution = ExecutionService(settings, ledger)
    executor = FakeExecutor()
    runner = ExecutionRunner(
        settings,
        ledger,
        execution,
        publisher,
        lambda _plan: executor,
        resource_providers=ResourceProviderRegistry((_TestResourceProvider(),)),
        script_cache=ScriptCacheService(
            settings.script_cache_dir,
            ledger,
            fetcher=lambda _url: script_content,
        ),
        now=lambda: NOW,
    )
    plan = with_plan_hash(
        _plan().model_copy(
            update={
                "script": _plan().script.model_copy(
                    update={
                        "sha256": Sha256(script_sha256),
                        "download_url": "https://master/scripts/test.zip",
                    }
                ),
            }
        )
    )
    ledger.claim_run(plan.run_id.root, plan.attempt_no, plan_id=plan.plan_id.root)

    async def execute_once() -> None:
        task = runner.start(plan, SESSION_ID)
        assert task is not None
        await task

    asyncio.run(execute_once())

    assert executor.calls == 1
    assert executor.script_path is not None
    assert not executor.script_path.exists()
    cached = ledger.get_cached_script(plan.script.script_id.root, plan.script.version, script_sha256)
    assert cached is not None and Path(cached.path).is_file()
