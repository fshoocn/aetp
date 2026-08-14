"""P3.8：任务类型插件数据接口测试（D-19）。

核心验收：插件与 Master 仅数据耦合——业务代码通过 TaskTypePlugin 端口
拿/收三类数据（case 列表、Shard 分割、结构化 case 结果），
不 import 任何 MQTT/FastAPI/DB 实现。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from aetp_protocol.capabilities import BusRequirement, HardwareRequirements, VehicleRequirement

from master.plugins import (
    CaseInfo,
    CaseResult,
    ExecutionPluginRegistry,
    PluginCapability,
    PluginLoadError,
    PluginNotFoundError,
    PluginRegistry,
    PluginVersionMismatchError,
    ShardSpec,
    TaskContext,
    TaskTypePlugin,
    filter_supported,
)


# ---------------------------------------------------------------------------
# duck-typed 示例插件（模拟 pytest 插件，不继承端口）
# ---------------------------------------------------------------------------


class PytestPlugin:
    task_type = "pytest"
    display_name = "pytest"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    config_schema: Mapping[str, Any] = {"type": "object", "properties": {}}
    upload_spec: Mapping[str, Any] = {"extensions": [".py", ".zip"], "max_size_mb": 10}
    parse_location = "master"
    result_parse_location = "agent"
    verify_location = "master"

    def verify_script(self, script_dir, config):
        """模拟 py_compile：空目录返回语法错误，否则通过。"""
        if config.get("broken"):
            return [f"{script_dir}/test_foo.py: 语法错误"]
        return []

    async def parse_cases(self, script_dir, config):
        """模拟 pytest --collect-only：stable_key = nodeid（跨版本稳定）。"""
        return [
            CaseInfo(
                stable_key="test_can_open_channel",
                name="test_can_open_channel",
                tags=("smoke",),
                estimated_duration_s=5.0,
            ),
            CaseInfo(
                stable_key="test_can_send_frame",
                name="test_can_send_frame",
                estimated_duration_s=8.0,
            ),
            CaseInfo(
                stable_key="test_can_close_channel",
                name="test_can_close_channel",
                estimated_duration_s=4.0,
            ),
        ]

    async def split_shards(self, cases, policy, config):
        """by_case_count：每 N 个 case 一个 Shard（子任务），各带专属执行参数。"""
        per = int(policy.get("cases_per_shard", 2))
        shards = []
        for shard_no, i in enumerate(range(0, len(cases), per)):
            group = cases[i : i + per]
            shards.append(
                ShardSpec(
                    case_keys=tuple(c.stable_key for c in group),
                    execution_params={"channel": shard_no},  # 每 Shard 专属执行参数（如不同 CAN 通道）
                )
            )
        return shards

    async def parse_results(self, artifact_files, context):
        return [
            CaseResult(case_key="test_can_open_channel", status="passed", duration_ms=1200)
        ]

    def hardware_requirements(self, config, cases):
        return HardwareRequirements(
            vehicle=VehicleRequirement(
                all_of=(BusRequirement(bus_type="can", minimum_channels=1),)
            )
        )


# ---------------------------------------------------------------------------
# 业务用例：只依赖 TaskTypePlugin 端口（数据耦合）
# ---------------------------------------------------------------------------


async def _run_parse_and_split_pipeline(plugin: TaskTypePlugin) -> tuple[int, int]:
    """业务用例：解析用例 → 按策略分割 → 统计（全程只经插件数据接口）。"""
    cases = await plugin.parse_cases("/data/scripts/S-1/", {})
    shards = await plugin.split_shards(
        cases, {"type": "by_case_count", "cases_per_shard": 2}, {}
    )
    return len(cases), len(shards)


# ---------------------------------------------------------------------------
# 插件数据接口契约测试
# ---------------------------------------------------------------------------


def test_plugin_metadata_fields():
    plugin: TaskTypePlugin = PytestPlugin()
    assert plugin.task_type == "pytest"
    assert plugin.display_name == "pytest"
    assert plugin.plugin_version == "1.0.0"
    assert plugin.supported_versions == frozenset({"1.0.0"})
    assert plugin.parse_location == "master"
    assert plugin.result_parse_location == "agent"
    assert plugin.upload_spec["extensions"] == [".py", ".zip"]


def test_plugin_verify_script_contract():
    """verify_script 编译/格式验证：错误列表（空=通过），失败脚本不进入解析队列。"""
    plugin: TaskTypePlugin = PytestPlugin()
    assert plugin.verify_script("/data/scripts/S-1/", {}) == []
    errors = plugin.verify_script("/data/scripts/S-1/", {"broken": True})
    assert len(errors) == 1
    assert "语法错误" in errors[0]


def test_plugin_parse_cases_contract():
    """parse_cases 返回 CaseInfo，stable_key 跨版本稳定。"""
    plugin: TaskTypePlugin = PytestPlugin()
    cases = asyncio.run(plugin.parse_cases("/data/scripts/S-1/", {}))
    assert len(cases) == 3
    assert [c.stable_key for c in cases] == [
        "test_can_open_channel",
        "test_can_send_frame",
        "test_can_close_channel",
    ]
    assert cases[0].tags == ("smoke",)
    assert cases[0].estimated_duration_s == 5.0


def test_plugin_split_shards_contract():
    """split_shards 按 by_case_count 返回子任务（ShardSpec）：case 集合 + 专属执行参数。"""
    plugin: TaskTypePlugin = PytestPlugin()
    cases = asyncio.run(plugin.parse_cases("/data/scripts/S-1/", {}))
    shards = asyncio.run(
        plugin.split_shards(cases, {"type": "by_case_count", "cases_per_shard": 2}, {})
    )
    assert [s.case_keys for s in shards] == [
        ("test_can_open_channel", "test_can_send_frame"),
        ("test_can_close_channel",),
    ]
    assert [s.execution_params for s in shards] == [{"channel": 0}, {"channel": 1}]


def test_plugin_parse_results_contract():
    """parse_results 返回结构化 CaseResult（D-19）。"""
    plugin: TaskTypePlugin = PytestPlugin()
    context = TaskContext(
        task_id="T-1",
        shard_id="SH-1",
        run_id="R-1",
        node_id="bench-001",
        params={},
        script_ref={"script_id": "S-1", "version": 1, "sha256": "a" * 64},
    )
    results = asyncio.run(
        plugin.parse_results(["data/artifacts/R-1/report.json"], context)
    )
    assert len(results) == 1
    assert results[0].case_key == "test_can_open_channel"
    assert results[0].status == "passed"
    assert results[0].duration_ms == 1200


def test_plugin_hardware_requirements_contract():
    """hardware_requirements 返回强类型需求（§18.5 节点匹配）。"""
    plugin: TaskTypePlugin = PytestPlugin()
    req = plugin.hardware_requirements({}, [])
    assert isinstance(req, HardwareRequirements)
    assert req.vehicle is not None
    assert req.vehicle.all_of[0].minimum_channels == 1


# ---------------------------------------------------------------------------
# PluginRegistry 契约测试
# ---------------------------------------------------------------------------


def test_registry_register_and_get():
    registry = PluginRegistry()
    registry.register(PytestPlugin())
    assert registry.get("pytest") is not None
    assert registry.get("missing") is None


def test_registry_duplicate_register_raises():
    registry = PluginRegistry()
    registry.register(PytestPlugin())
    with pytest.raises(ValueError, match="任务类型已注册"):
        registry.register(PytestPlugin())


def test_registry_list_sorted():
    class APlugin:
        task_type = "a"
        display_name = "a"
        plugin_version = "1.0.0"
        supported_versions = frozenset({"1.0.0"})
        config_schema: Mapping[str, Any] = {}
        upload_spec: Mapping[str, Any] = {}
        parse_location = "master"
        result_parse_location = "master"
        verify_location = "master"

        def verify_script(self, script_dir, config):
            return []

        async def parse_cases(self, script_dir, config):
            return []

        async def split_shards(self, cases, policy, config):
            return []

        async def parse_results(self, artifact_files, context):
            return []

        def hardware_requirements(self, config, cases):
            return HardwareRequirements()

    class BPlugin(APlugin):
        task_type = "b"

    registry = PluginRegistry()
    registry.register(BPlugin())
    registry.register(APlugin())
    assert [p.task_type for p in registry.list()] == ["a", "b"]


def test_registry_version_compatibility():
    """版本兼容校验（§18.2）：plugin_version 须在 supported_versions 内。"""
    registry = PluginRegistry()
    registry.register(PytestPlugin())
    assert registry.is_version_compatible("pytest", "1.0.0") is True
    assert registry.is_version_compatible("pytest", "2.0.0") is False
    assert registry.is_version_compatible("missing", "1.0.0") is False


def test_registry_require_not_found():
    """require 未注册任务类型 → PluginNotFoundError（PLUGIN_NOT_FOUND）。"""
    registry = PluginRegistry()
    with pytest.raises(PluginNotFoundError) as exc:
        registry.require("missing")
    assert exc.value.code == "PLUGIN_NOT_FOUND"


def test_registry_require_compatible_mismatch():
    """require_compatible 版本不兼容 → PluginVersionMismatchError（PLUGIN_VERSION_MISMATCH）。"""
    registry = PluginRegistry()
    registry.register(PytestPlugin())  # supported_versions = {1.0.0}
    assert registry.require_compatible("pytest", "1.0.0").task_type == "pytest"
    with pytest.raises(PluginVersionMismatchError) as exc:
        registry.require_compatible("pytest", "2.0.0")
    assert exc.value.code == "PLUGIN_VERSION_MISMATCH"


# ---------------------------------------------------------------------------
# Agent 侧执行插件注册表与加载错误处理（P3.8 增强）
# ---------------------------------------------------------------------------


class CanTestExecutionPlugin:
    task_type = "can_test"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = "CAN"
    verify_location = "agent"  # CANoe 类验证依赖 COM，须在 Agent 端

    def verify_script(self, script_dir, config):
        if config.get("broken"):
            return [f"{script_dir}: 工程结构错误"]
        return []

    async def execute(self, context):
        return {"status": "passed"}

    async def cancel(self):
        pass

    async def parse_cases(self, script_dir, config):
        return []

    async def parse_results(self, artifact_files, context):
        return []


class PytestExecutionPlugin:
    task_type = "pytest"
    plugin_version = "1.0.0"
    supported_versions = frozenset({"1.0.0"})
    display_name = "pytest"
    verify_location = "master"

    def verify_script(self, script_dir, config):
        return []

    async def execute(self, context):
        return {"status": "passed"}

    async def cancel(self):
        pass

    async def parse_cases(self, script_dir, config):
        return []

    async def parse_results(self, artifact_files, context):
        return []


def test_execution_registry_register_and_require():
    registry = ExecutionPluginRegistry()
    registry.register(CanTestExecutionPlugin())
    plugin = registry.require("can_test")
    assert plugin.plugin_version == "1.0.0"
    assert registry.supported_task_types() == ["can_test"]


def test_execution_registry_missing_plugin_raises():
    """Agent 未安装该 task_type 插件：run.assign 应被拒绝（PLUGIN_NOT_FOUND）。"""
    registry = ExecutionPluginRegistry()
    with pytest.raises(PluginNotFoundError, match="未安装") as exc:
        registry.require("flash_test")
    assert exc.value.code == "PLUGIN_NOT_FOUND"


def test_execution_registry_version_mismatch_raises():
    """run.assign 声明版本与本地插件不兼容 → PLUGIN_VERSION_MISMATCH。"""
    registry = ExecutionPluginRegistry()
    registry.register(CanTestExecutionPlugin())
    assert registry.require_compatible("can_test", "1.0.0") is not None
    with pytest.raises(PluginVersionMismatchError, match="不兼容") as exc:
        registry.require_compatible("can_test", "0.9.0")
    assert exc.value.code == "PLUGIN_VERSION_MISMATCH"


# ---------------------------------------------------------------------------
# 插件能力主动上报 + Master 端筛查（P3.8 增强）
# ---------------------------------------------------------------------------


def test_execution_registry_capabilities_and_revision():
    """capabilities() 汇总插件能力清单；revision 记录清单变动（主动上报依据）。"""
    registry = ExecutionPluginRegistry()
    assert registry.revision == 0
    registry.register(CanTestExecutionPlugin())
    assert registry.revision == 1
    caps = registry.capabilities()
    assert len(caps) == 1
    c = caps[0]
    assert c.task_type == "can_test"
    assert c.plugin_version == "1.0.0"
    assert c.supported_versions == frozenset({"1.0.0"})
    assert c.verify_location == "agent"  # CANoe 类验证须在 Agent 端
    assert c.supports("1.0.0") is True
    assert c.supports("0.9.0") is False
    assert c.supports() is True  # 未指定版本 = 任意版本兼容


def test_agent_side_verify_script():
    """verify_location=agent 时，验证在 Agent 端执行并回传错误列表。"""
    registry = ExecutionPluginRegistry()
    registry.register(CanTestExecutionPlugin())
    plugin = registry.require_compatible("can_test", "1.0.0")
    assert plugin.verify_location == "agent"
    # Master 下发 script.verify 后，Agent 端调用 verify_script
    assert plugin.verify_script("/data/scripts/S-1/", {}) == []
    errors = plugin.verify_script("/data/scripts/S-1/", {"broken": True})
    assert errors == ["/data/scripts/S-1/: 工程结构错误"]


def test_capability_change_detection():
    """监测到插件清单变动（revision 递增）→ 主动上报。"""
    registry = ExecutionPluginRegistry()
    registry.register(CanTestExecutionPlugin())
    first = registry.revision
    registry.register(PytestExecutionPlugin())
    assert registry.revision == first + 1


def test_master_filter_supported_capabilities():
    """Master 端调度前筛查：从多 Agent 能力中筛出支持指定 task_type+version 者。"""
    agents = [
        PluginCapability(
            task_type="can_test", plugin_version="1.0.0",
            supported_versions=frozenset({"1.0.0"}), display_name="CAN",
        ),
        PluginCapability(
            task_type="can_test", plugin_version="2.0.0",
            supported_versions=frozenset({"1.0.0", "2.0.0"}), display_name="CAN",
        ),
        PluginCapability(
            task_type="flash_test", plugin_version="1.0.0",
            supported_versions=frozenset({"1.0.0"}), display_name="Flash",
        ),
    ]
    # 只按 task_type
    can_agents = filter_supported(agents, "can_test")
    assert len(can_agents) == 2
    # 指定版本：只有兼容 1.0.0 的（两个都兼容）
    assert len(filter_supported(agents, "can_test", "1.0.0")) == 2
    # 指定不兼容版本：筛出 0
    assert filter_supported(agents, "can_test", "3.0.0") == []
    # flash_test
    assert len(filter_supported(agents, "flash_test")) == 1


# ---------------------------------------------------------------------------
# 插件与 Master 仅数据耦合（P3.8 核心验收）
# ---------------------------------------------------------------------------


def test_business_uses_plugin_port_data_only():
    """业务用例只经 TaskTypePlugin 端口拿三类数据，注入 duck-typed 插件即可工作。"""
    plugin: TaskTypePlugin = PytestPlugin()
    case_count, shard_count = asyncio.run(_run_parse_and_split_pipeline(plugin))
    assert case_count == 3
    assert shard_count == 2


# ---------------------------------------------------------------------------
# Entry Points 自动发现（§10.6 受信任扩展，对比 uvicorn import_from_string）
# ---------------------------------------------------------------------------


def test_registry_discover_from_entry_points(monkeypatch):
    """从已安装受信任包 entry points 自动发现并注册插件，无需改容器代码。"""
    registry = PluginRegistry()
    ep = SimpleNamespace(
        name="pytest",
        value="tests.test_plugins:PytestPlugin",
        load=lambda: PytestPlugin(),
    )
    monkeypatch.setattr(
        "master.plugins.registry.entry_points",
        lambda group=None: [ep] if group == "aetp.plugins" else [],
    )
    count = registry.discover("aetp.plugins")
    assert count == 1
    assert registry.get("pytest") is not None
    # 重复 discover 会因重复注册抛错（幂等由调用方去重或容忍）
    with pytest.raises(ValueError, match="已注册"):
        registry.discover("aetp.plugins")


def test_execution_registry_discover_and_revision(monkeypatch):
    """Agent 侧 entry points 发现：注册即变更（revision 递增，触发主动上报）。"""
    registry = ExecutionPluginRegistry()
    ep = SimpleNamespace(
        name="can_test",
        value="tests.test_plugins:CanTestExecutionPlugin",
        load=lambda: CanTestExecutionPlugin(),
    )
    monkeypatch.setattr(
        "master.plugins.execution.entry_points",
        lambda group=None: [ep] if group == "aetp.execution_plugins" else [],
    )
    registry.discover("aetp.execution_plugins")
    assert registry.revision == 1
    caps = registry.capabilities()
    assert caps[0].task_type == "can_test"
    assert caps[0].verify_location == "agent"


def test_discover_load_failure_raises(monkeypatch):
    """entry point 加载失败 → PluginLoadError（PLUGIN_LOAD_FAILED）。"""
    registry = PluginRegistry()

    def broken_load():
        raise ImportError("broken plugin")

    ep = SimpleNamespace(name="broken", value="x:Y", load=broken_load)
    monkeypatch.setattr(
        "master.plugins.registry.entry_points",
        lambda group=None: [ep] if group == "aetp.plugins" else [],
    )
    with pytest.raises(PluginLoadError, match="插件加载失败") as exc:
        registry.discover("aetp.plugins")
    assert exc.value.code == "PLUGIN_LOAD_FAILED"
