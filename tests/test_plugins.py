"""P3.8/P5.5：Master 侧任务类型插件契约测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from aetp_protocol.capabilities import (
    BusRequirement,
    HardwareRequirements,
    VehicleRequirement,
)
from aetp_protocol.plugin import AgentPackageSpec, PluginMetadata, PluginPackage
from master.plugins import (
    CaseInfo,
    PluginLoadError,
    MasterTaskPlugin,
    PluginNotFoundError,
    PluginRegistry,
    PluginVersionMismatchError,
    ShardSpec,
    TaskDefinitionSpec,
)
from agent.plugins.execution import AgentPluginRegistry


class MasterFakePlugin:
    task_type = "master_fake"
    display_name = "Master Fake"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    config_schema: Mapping[str, Any] = {"type": "object"}
    upload_spec: Mapping[str, Any] = {"extensions": [".fake"]}

    def verify_script(self, script_dir, config):
        return ["invalid script"] if config.get("broken") else []

    async def parse_cases(self, script_dir, config):
        return [
            CaseInfo(stable_key="case-a", name="Case A", estimated_duration_s=5),
            CaseInfo(stable_key="case-b", name="Case B", estimated_duration_s=8),
            CaseInfo(stable_key="case-c", name="Case C", estimated_duration_s=4),
        ]

    async def split_shards(self, cases, policy, config):
        per_shard = int(policy.get("cases_per_shard", 2))
        return [
            ShardSpec(
                case_keys=tuple(case.stable_key for case in cases[index : index + per_shard]),
                execution_params={"shard": index // per_shard},
            )
            for index in range(0, len(cases), per_shard)
        ]

    def build_task_definition(self, config, cases):
        return TaskDefinitionSpec(
            default_case_keys=tuple(case.stable_key for case in cases),
            parameter_schema=dict(self.config_schema),
            split_policy={"type": "by_case_count", "cases_per_shard": 2},
            hardware_requirements=self.hardware_requirements(config, cases),
        )

    def result_schema(self, config):
        return {"type": "object", "required": ["passed", "case_results"]}

    def hardware_requirements(self, config, cases):
        return HardwareRequirements(
            vehicle=VehicleRequirement(
                all_of=(BusRequirement(bus_type="can", minimum_channels=1),)
            )
        )


async def _parse_and_split(plugin: MasterTaskPlugin) -> tuple[int, int]:
    cases = await plugin.parse_cases("/data/scripts/S-1", {})
    shards = await plugin.split_shards(cases, {"cases_per_shard": 2}, {})
    return len(cases), len(shards)


def test_master_plugin_contract() -> None:
    plugin: MasterTaskPlugin = MasterFakePlugin()
    assert plugin.task_type == "master_fake"
    assert plugin.verify_script("/script", {}) == []
    assert plugin.verify_script("/script", {"broken": True}) == ["invalid script"]


def test_master_plugin_parse_split_and_hardware_contract() -> None:
    plugin: MasterTaskPlugin = MasterFakePlugin()
    assert asyncio.run(_parse_and_split(plugin)) == (3, 2)
    requirement = plugin.hardware_requirements({}, [])
    assert requirement.vehicle is not None
    assert requirement.vehicle.all_of[0].minimum_channels == 1


def test_master_plugin_task_definition_contract() -> None:
    plugin: MasterTaskPlugin = MasterFakePlugin()
    cases = asyncio.run(plugin.parse_cases("/script", {}))
    task_definition = plugin.build_task_definition({}, cases)
    assert task_definition.default_case_keys == ("case-a", "case-b", "case-c")
    assert task_definition.split_policy["type"] == "by_case_count"
    assert plugin.result_schema({})["required"] == ["passed", "case_results"]


def test_registry_register_get_list_and_duplicate() -> None:
    registry = PluginRegistry()
    plugin = MasterFakePlugin()
    package = PluginPackage(
        metadata=PluginMetadata(
            task_type="master_fake",
            plugin_version="1.0.0",
            supported_versions=frozenset({"1.0.0"}),
        ),
        master=plugin,
        agent=object(),
    )
    registry.register(package)
    assert registry.get("master_fake") is package
    assert [item.metadata.task_type for item in registry.list()] == ["master_fake"]
    with pytest.raises(ValueError, match="任务类型已注册"):
        registry.register(package)


def test_registry_version_compatibility_and_errors() -> None:
    registry = PluginRegistry()
    registry.register(
        PluginPackage(
            metadata=PluginMetadata(
                task_type="master_fake",
                plugin_version="1.0.0",
                supported_versions=frozenset({"1.0.0"}),
            ),
            master=MasterFakePlugin(),
            agent=object(),
        )
    )
    assert registry.is_version_compatible("master_fake", "1.0.0") is True
    assert registry.is_version_compatible("master_fake", "2.0.0") is False
    with pytest.raises(PluginNotFoundError) as missing:
        registry.require("missing")
    assert missing.value.code == "PLUGIN_NOT_FOUND"
    with pytest.raises(PluginVersionMismatchError) as mismatch:
        registry.require_compatible("master_fake", "2.0.0")
    assert mismatch.value.code == "PLUGIN_VERSION_MISMATCH"


def test_registry_delegates_master_task_generation_and_result_schema() -> None:
    registry = PluginRegistry()
    registry.register(
        PluginPackage(
            metadata=PluginMetadata(
                task_type="master_fake",
                plugin_version="1.0.0",
                supported_versions=frozenset({"1.0.0"}),
            ),
            master=MasterFakePlugin(),
            agent=object(),
        )
    )
    cases = asyncio.run(
        registry.require("master_fake").master.parse_cases("/script", {})
    )

    definition = registry.build_task_definition("master_fake", {}, cases)

    assert definition.default_case_keys == ("case-a", "case-b", "case-c")
    assert registry.result_schema("master_fake", {})["required"] == [
        "passed",
        "case_results",
    ]


def test_registry_builds_agent_package_ref() -> None:
    class PackagedPlugin(MasterFakePlugin):
        task_type = "packaged"
        plugin_version = "1.2.0"
        supported_versions = frozenset({"1.2.0"})

    package = PluginPackage(
        metadata=PluginMetadata(
            task_type="packaged",
            plugin_version="1.2.0",
            supported_versions=frozenset({"1.2.0"}),
            agent_package=AgentPackageSpec(
                package_name="aetp-plugin-packaged",
                version="1.2.0",
                download_url="https://master.example/packaged.whl",
                sha256="a" * 64,
                entry_point="aetp_packaged:Plugin",
            ),
        ),
        master=PackagedPlugin(),
        agent=object(),
    )

    registry = PluginRegistry()
    registry.register(package)
    ref = registry.agent_package_ref("packaged")
    assert ref is not None
    assert ref.task_type == "packaged"
    assert ref.version == "1.2.0"
    assert ref.entry_point == "aetp_packaged:Plugin"


class AgentSurface:
    task_type = "shared_task"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = "Shared Task"

    async def execute(self, context):
        return {"passed": True}

    async def cancel(self):
        return None

    async def collect_logs(self, context):
        return None

    async def analyze_results(self, execution_result, context):
        return execution_result


def test_same_plugin_package_is_registered_on_master_and_agent() -> None:
    master_surface = MasterFakePlugin()
    master_surface.task_type = "shared_task"
    package = PluginPackage(
        metadata=PluginMetadata(
            task_type="shared_task",
            plugin_version="1.0.0",
            supported_versions=frozenset({"1.0.0"}),
            display_name="Shared Task",
            agent_package=AgentPackageSpec(
                package_name="aetp-shared-task",
                version="1.0.0",
                download_url="https://master.example/shared.whl",
                sha256="a" * 64,
                entry_point="aetp_shared:AgentPlugin",
            ),
        ),
        master=master_surface,
        agent=AgentSurface(),
    )

    master_registry = PluginRegistry()
    agent_registry = AgentPluginRegistry()
    master_registry.register(package)
    agent_registry.register_package(package)

    assert master_registry.require("shared_task").master is master_surface
    assert agent_registry.require("shared_task").task_type == "shared_task"
    assert master_registry.require_compatible("shared_task", "1.0.0")
    assert agent_registry.require_compatible("shared_task", "1.0.0")
    assert master_registry.agent_package_ref("shared_task").version == "1.0.0"


def test_shared_plugin_package_rejects_metadata_version_mismatch() -> None:
    master_surface = MasterFakePlugin()
    master_surface.task_type = "mismatch_task"
    package = PluginPackage(
        metadata=PluginMetadata(
            task_type="mismatch_task",
            plugin_version="2.0.0",
            supported_versions=frozenset({"2.0.0"}),
        ),
        master=master_surface,
        agent=AgentSurface(),
    )

    with pytest.raises(ValueError, match="metadata 与 Master 入口不一致"):
        PluginRegistry().register(package)


def test_registry_discovers_trusted_master_entry_point(monkeypatch) -> None:
    registry = PluginRegistry()
    entry_point = SimpleNamespace(
        name="master_fake",
        value="tests.test_plugins:PluginPackage",
        load=lambda: PluginPackage(
            metadata=PluginMetadata(
                task_type="master_fake",
                plugin_version="1.0.0",
                supported_versions=frozenset({"1.0.0"}),
            ),
            master=MasterFakePlugin(),
            agent=object(),
        ),
    )
    monkeypatch.setattr(
        "master.plugins.registry.entry_points",
        lambda group=None: [entry_point] if group == "aetp.plugins" else [],
    )
    assert registry.discover("aetp.plugins") == 1
    assert registry.get("master_fake") is not None


def test_registry_discover_load_failure(monkeypatch) -> None:
    registry = PluginRegistry()

    def broken_load():
        raise ImportError("broken plugin")

    entry_point = SimpleNamespace(name="broken", value="x:Y", load=broken_load)
    monkeypatch.setattr(
        "master.plugins.registry.entry_points",
        lambda group=None: [entry_point] if group == "aetp.plugins" else [],
    )
    with pytest.raises(PluginLoadError, match="插件加载失败"):
        registry.discover("aetp.plugins")
