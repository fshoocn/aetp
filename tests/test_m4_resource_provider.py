"""M4 ResourceProvider activate/deactivate 生命周期测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from aetp_protocol.execution import PlanResourceBinding
from aetp_protocol.ids import BusinessId
from aetp_protocol.plan_hash import with_plan_hash

from agent.adapters.sqlite.ledger import SQLiteLedger
from agent.application.services.capability_publisher import CapabilityPublisher
from agent.application.services.execution_runner import ExecutionRunner
from agent.application.services.execution_service import ExecutionService
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.config import AgentSettings
from agent.plugins.registry import PluginRegistry
from tests.test_agent_capability_publisher import FakeTransport
from tests.test_m3_plan_lease import NOW, SESSION_ID, _plan


class _Provider:
    resource_type = "can"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.activated: list[str] = []
        self.deactivated: list[str] = []

    async def activate(self, binding: PlanResourceBinding) -> None:
        if self.fail:
            raise RuntimeError("hardware unavailable")
        self.activated.append(binding.resource_id.root)

    async def deactivate(self, binding: PlanResourceBinding) -> None:
        self.deactivated.append(binding.resource_id.root)


class _Executor:
    plugin_version = "2.0.0"

    async def execute(self, context):
        return {"passed": True}

    async def cancel(self):
        return None


def _runner(tmp_path, provider_registry: ResourceProviderRegistry) -> tuple[ExecutionRunner, SQLiteLedger]:
    settings = AgentSettings(
        node_id="01J00000000000000000000000",
        name="Bench",
        master_id="aetp-master",
        mqtt_client_id="agent-bench",
        mqtt_use_tls=False,
        plugin_dir=tmp_path / "plugins",
    )
    ledger = SQLiteLedger(f"sqlite:///{tmp_path / 'agent.db'}")
    publisher = CapabilityPublisher(FakeTransport(), settings, PluginRegistry())
    return (
        ExecutionRunner(
            settings,
            ledger,
            ExecutionService(settings, ledger),
            publisher,
            lambda _plan: _Executor(),
            resource_providers=provider_registry,
            now=lambda: NOW,
        ),
        ledger,
    )


def test_v2_runner_activates_and_deactivates_resources(tmp_path) -> None:
    provider = _Provider()
    registry = ResourceProviderRegistry((provider,))
    runner, ledger = _runner(tmp_path, registry)
    plan = with_plan_hash(
        _plan().model_copy(
            update={
                "created_at": NOW - timedelta(seconds=1),
                "resource_bindings": (
                    PlanResourceBinding(
                        lease_id=BusinessId("01J00000000000000000000070"),
                        resource_id=BusinessId("01J00000000000000000000071"),
                        resource_type="can",
                        lease_revision=1,
                        expires_at=NOW + timedelta(minutes=5),
                    ),
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

    assert provider.activated == ["01J00000000000000000000071"]
    assert provider.deactivated == ["01J00000000000000000000071"]


def test_v2_runner_maps_activation_failure_to_stable_error(tmp_path) -> None:
    provider = _Provider(fail=True)
    runner, ledger = _runner(tmp_path, ResourceProviderRegistry((provider,)))
    plan = with_plan_hash(
        _plan().model_copy(
            update={
                "created_at": NOW - timedelta(seconds=1),
                "resource_bindings": (
                    PlanResourceBinding(
                        lease_id=BusinessId("01J00000000000000000000072"),
                        resource_id=BusinessId("01J00000000000000000000073"),
                        resource_type="can",
                        lease_revision=1,
                        expires_at=NOW + timedelta(minutes=5),
                    ),
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

    from datetime import UTC, datetime

    entries = ledger.claim_due_outbox(20, datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1))
    finished = [entry for entry in entries if entry.topic.endswith("/execution.finished")]
    assert len(finished) == 1
    from aetp_protocol.envelope import parse_message

    _envelope, payload = parse_message(finished[0].payload)
    assert payload.result.error is not None
    assert payload.result.error.code.root == "RESOURCE_ACTIVATION_FAILED"
    assert provider.deactivated == []


def test_v2_runner_rejects_resource_without_provider(tmp_path) -> None:
    runner, ledger = _runner(tmp_path, ResourceProviderRegistry())
    plan = with_plan_hash(
        _plan().model_copy(
            update={
                "created_at": NOW - timedelta(seconds=1),
            }
        )
    )
    ledger.claim_run(plan.run_id.root, plan.attempt_no, plan_id=plan.plan_id.root)

    async def execute_once() -> None:
        task = runner.start(plan, SESSION_ID)
        assert task is not None
        await task

    asyncio.run(execute_once())

    entries = ledger.claim_due_outbox(
        20,
        datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1),
    )
    finished = [entry for entry in entries if entry.topic.endswith("/execution.finished")]
    assert len(finished) == 1
    from aetp_protocol.envelope import parse_message

    _envelope, payload = parse_message(finished[0].payload)
    assert payload.result.error is not None
    assert payload.result.error.code.root == "RESOURCE_ACTIVATION_FAILED"
