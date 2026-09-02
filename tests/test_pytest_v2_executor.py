"""pytest V2 executor 真实归档、执行和 JUnit Artifact 测试。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from aetp_protocol.ids import MessageId, Sha256
from aetp_protocol.payloads import ExecutionFinished
from aetp_protocol.plan_hash import with_plan_hash
from aetp_protocol.plugin_types import PluginDistributionRef
from aetp_protocol.v2_envelope import parse_v2_message

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.execution_service import ExecutionService
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.application.services.script_cache_service import ScriptCacheService
from agent.application.services.v2_capability_publisher import AgentV2CapabilityPublisher
from agent.application.services.v2_execution_runner import V2ExecutionRunner
from agent.application.services.v2_executor_resolver import V2ExecutorResolver
from agent.config import AgentSettings
from agent.plugins.v2_installer import V2PluginInstaller
from agent.plugins.v2_registry import AgentV2PluginRegistry
from common.transport import Transport
from tests.test_agent_v2_capability_publisher import FakeTransport
from tests.test_m3_plan_lease import NOW, SESSION_ID, _plan


class _TestResourceProvider:
    provider_id = "org.test.resource"
    resource_type = "can"

    def discover(self):
        return ()

    async def activate(self, binding) -> None:
        del binding

    async def deactivate(self, binding) -> None:
        del binding


class _ArtifactUploader:
    async def upload(
        self,
        url: str,
        path: str | Path,
        *,
        kind: str,
        filename: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, object]:
        del url, path, max_attempts
        return {
            "artifact_id": "01J00000000000000000000090",
            "kind": kind,
            "filename": filename or "artifact",
            "content_type": "application/xml",
            "size": 1,
            "sha256": "d" * 64,
        }


def _plugin_archive() -> bytes:
    root = Path(__file__).parents[1] / "plugins" / "pytest_plugin"
    manifest = json.loads((root / "plugin-v2.json").read_text(encoding="utf-8"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", json.dumps(manifest, separators=(",", ":")))
        for side in ("agent", "master"):
            archive.write(root / side / "v2_executor.py", f"{side}/v2_executor.py")
    return buffer.getvalue()


def _script_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("test_sample.py", "def test_sample():\n    assert True\n")
    return buffer.getvalue()


def test_pytest_v2_archive_installs_and_executes_with_artifact(tmp_path) -> None:
    plugin_data = _plugin_archive()
    plugin_sha = hashlib.sha256(plugin_data).hexdigest()
    installer = V2PluginInstaller(
        tmp_path / "plugins",
        fetcher=lambda _url: plugin_data,
    )
    installed = installer.install(
        PluginDistributionRef(
            plugin_id="org.pytest.executor",
            version="2.0.0",
            archive_sha256=plugin_sha,
            download_url="https://master/plugin.zip",
        )
    )
    registry = AgentV2PluginRegistry()
    registry.register(installed)
    resolver = V2ExecutorResolver(registry)

    script_data = _script_archive()
    script_sha = hashlib.sha256(script_data).hexdigest()
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    settings = AgentSettings(
        node_id="01J00000000000000000000000",
        name="pytest-v2-bench",
        master_id="aetp-master",
        mqtt_client_id="pytest-v2-agent",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
        script_cache_dir=tmp_path / "scripts",
    )
    publisher = AgentV2CapabilityPublisher(
        cast(Transport, FakeTransport()),
        settings,
        registry,
    )
    runner = V2ExecutionRunner(
        settings,
        ledger,
        ExecutionService(settings, ledger),
        publisher,
        resolver.resolve,
        resource_providers=ResourceProviderRegistry((_TestResourceProvider(),)),
        script_cache=ScriptCacheService(
            settings.script_cache_dir,
            ledger,
            fetcher=lambda _url: script_data,
        ),
        artifact_uploader=_ArtifactUploader(),
        now=lambda: NOW,
    )
    plan = with_plan_hash(
        _plan().model_copy(
            update={
                "created_at": NOW - timedelta(seconds=1),
                "case_keys": ("test_sample.py::test_sample",),
                "script": _plan().script.model_copy(
                    update={
                        "sha256": Sha256(script_sha),
                        "download_url": "https://master/script.zip",
                    }
                ),
                "artifact_upload_url": "https://master/artifacts",
            }
        )
    )
    ledger.claim_run(
        plan.run_id.root,
        plan.attempt_no,
        plan_id=plan.plan_id.root,
        shard_id=plan.shard_id.root,
        attempt_id=plan.attempt_id.root,
        plan_hash=plan.plan_hash.root,
    )

    async def execute_once() -> None:
        task = runner.start(plan, SESSION_ID, correlation_id=MessageId("pytest-v2-plan-0001"))
        assert task is not None
        await task

    asyncio.run(execute_once())
    entries = ledger.claim_due_outbox(
        50,
        datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1),
    )
    finished_entries = [entry for entry in entries if entry.topic.endswith("/execution.finished")]
    assert len(finished_entries) == 1
    _envelope, finished = parse_v2_message(finished_entries[0].payload)
    assert isinstance(finished, ExecutionFinished)
    assert finished.result.passed is True
    assert finished.result.case_results[0].case_key == "test_sample.py::test_sample"
    assert len(finished.result.artifacts) == 1
    assert finished.result.artifacts[0].kind.value == "report"
