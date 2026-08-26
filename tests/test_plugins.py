"""P3.8/P5.5：Master 侧任务类型插件契约测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from aetp_protocol.capabilities import (
    BusRequirement,
    HardwareRequirements,
    VehicleRequirement,
)
from aetp_protocol.plugin import AgentPackageSpec, PluginMetadata, PluginPackage

from agent.plugins.execution import AgentPluginRegistry
from master.plugins import (
    CaseInfo,
    MasterTaskPlugin,
    PluginLoadError,
    PluginNotFoundError,
    PluginRegistry,
    PluginVersionMismatchError,
    ShardSpec,
    TaskDefinitionSpec,
)


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
            vehicle=VehicleRequirement(all_of=(BusRequirement(bus_type="can", minimum_channels=1),))
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


def test_plugin_metadata_keeps_ui_config_page_metadata() -> None:
    metadata = PluginMetadata(
        task_type="ui_plugin",
        plugin_version="1.0.0",
        supported_versions=frozenset({"1.0.0"}),
        ui={"config_page": "ui_plugin", "min_frontend_version": "0.1.0"},
    )
    assert metadata.ui["config_page"] == "ui_plugin"
    assert metadata.ui["min_frontend_version"] == "0.1.0"


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
    cases = asyncio.run(registry.require("master_fake").master.parse_cases("/script", {}))

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


def test_plugin_manager_load_packages_injects_agent_package(tmp_path):
    """已安装 ZIP 插件 load_packages 时注入 agent_package（含签名下载 URL）。"""

    from master.plugins.manager import PluginManager

    # 构造一个最小 ZIP 插件（plugin.json + main.py）
    zip_bytes = _build_test_plugin_zip()

    manager = PluginManager(
        tmp_path,
        agent_download_builder=lambda plugin_id: f"https://master.example/internal/plugins/{plugin_id}/download",
    )
    record = manager.upload("test_plugin.zip", zip_bytes)
    assert record.task_type == "zip_test"
    manager.install(record.plugin_id)

    packages = manager.load_packages()
    assert len(packages) == 1
    package = packages[0]
    assert package.metadata.agent_package is not None
    assert package.metadata.agent_package.sha256 == record.sha256
    assert package.metadata.agent_package.version == "1.0.0"
    assert package.metadata.agent_package.entry_point == "main.py:package"
    assert record.plugin_id in package.metadata.agent_package.download_url


def test_plugin_download_endpoint_signed(client, tmp_path):
    """内部插件下载端点：签名 URL 可下载已安装插件 ZIP（Agent 侧用）。"""
    import io
    import zipfile

    container = client.app.state.container
    manager = container.plugin_manager()
    # 使用临时 PluginManager 根目录，避免测试移动/清空真实 master/data/plugins。
    original_paths = (
        manager.root,
        manager.archives,
        manager.install_dir,
        manager.manifest_path,
    )
    root = tmp_path / "plugins"
    manager.root = root
    manager.archives = root / "archives"
    manager.install_dir = root / "packages"
    manager.manifest_path = root / "manifest.json"
    manager.archives.mkdir(parents=True, exist_ok=True)
    manager.install_dir.mkdir(parents=True, exist_ok=True)
    try:
        zip_bytes = _build_test_plugin_zip()
        record = manager.upload("test_plugin.zip", zip_bytes)
        manager.install(record.plugin_id)

        download_service = container.plugin_download_service()
        url = download_service.build_download_url(record.plugin_id)
        # url 形如 /api/v1/internal/plugins/{id}/download?expires=...&signature=...
        path = url[url.index("/api/v1") :]

        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers["X-Checksum-Sha256"] == record.sha256
        content = resp.content
        # 校验内容确实是 zip 且含 main.py
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            assert "main.py" in archive.namelist()

        # 错误签名被拒
        bad = client.get(
            f"/api/v1/internal/plugins/{record.plugin_id}/download?expires=9999999999&signature={'0' * 64}"
        )
        assert bad.status_code == 403
    finally:
        (
            manager.root,
            manager.archives,
            manager.install_dir,
            manager.manifest_path,
        ) = original_paths


def test_plugin_manager_restores_installed_plugin_after_restart(tmp_path):
    """重建 Manager 后应从 manifest/packages 恢复已安装插件。"""
    from master.plugins.manager import PluginManager

    manager = PluginManager(tmp_path)
    record = manager.upload("test_plugin.zip", _build_test_plugin_zip())
    manager.install(record.plugin_id)

    restarted = PluginManager(tmp_path)
    packages = restarted.load_packages()

    assert restarted.manifest_path.is_file()
    assert [package.metadata.task_type for package in packages] == ["zip_test"]


def test_plugin_manager_keeps_task_type_enabled_when_only_old_version_disabled(tmp_path):
    """停用旧版本不应屏蔽同 task_type 的已启用新版本。"""
    from master.plugins.manager import PluginManager

    manager = PluginManager(tmp_path)
    first = manager.upload("test_plugin.zip", _build_test_plugin_zip())
    manager.install(first.plugin_id)
    manager.set_enabled(first.plugin_id, False)

    second_bytes = _build_test_plugin_zip("1.1.0")
    second = manager.upload("test_plugin.zip", second_bytes)
    manager.install(second.plugin_id)

    assert manager.disabled_task_types() == set()


def test_plugin_manager_disables_previous_version_when_new_version_is_installed(tmp_path):
    """安装启用的新版本后，同 task_type 的旧版本应自动停用。"""
    from master.plugins.manager import PluginManager

    manager = PluginManager(tmp_path)
    first = manager.upload("test_plugin.zip", _build_test_plugin_zip())
    manager.install(first.plugin_id)

    second = manager.upload("test_plugin.zip", _build_test_plugin_zip("1.1.0"))
    manager.install(second.plugin_id)

    records = {item.plugin_id: item for item in manager.list()}
    assert records[first.plugin_id].enabled is False
    assert records[second.plugin_id].enabled is True
    assert [package.metadata.plugin_version for package in manager.load_packages()] == ["1.1.0"]


def test_managed_plugin_download_endpoint_returns_installed_zip(client):
    """平台管理员可下载已安装插件包，未认证请求不可下载。"""

    manager = client.app.state.container.plugin_manager()
    record = manager.upload("test_plugin.zip", _build_test_plugin_zip())
    manager.install(record.plugin_id)

    denied = client.get(f"/api/v1/task-types/managed/{record.plugin_id}/download")
    assert denied.status_code == 401

    auth_service = client.app.state.container.auth_service()
    assert auth_service.bootstrap_admin("plugin-download-admin", "admin-pass-123", "Plugin Admin")
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "plugin-download-admin", "password": "admin-pass-123"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = client.get(
        f"/api/v1/task-types/managed/{record.plugin_id}/download",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert record.filename in response.headers["content-disposition"]
    assert response.content.startswith(b"PK")


def _build_test_plugin_zip(version: str = "1.0.0") -> bytes:
    """构造最小 ZIP 插件包：plugin.json + main.py（导出 package）。"""
    import io
    import zipfile

    plugin_json = f'{{"task_type": "zip_test", "plugin_version": "{version}", "display_name": "Zip Test"}}'
    main_py = (
        "from aetp_protocol.plugin import PluginMetadata, PluginPackage\n"
        "from aetp_protocol.capabilities import HardwareRequirements\n"
        "class M:\n"
        "    task_type='zip_test'; display_name='Zip Test'\n"
        f"    plugin_version='{version}'; supported_versions=frozenset({{'{version}'}})\n"
        "    config_schema={}; upload_spec={}\n"
        "    def verify_script(self, d, c): return []\n"
        "    async def parse_cases(self, d, c): return []\n"
        "    async def split_shards(self, c, p, cfg): return []\n"
        "    def build_task_definition(self, c, cs): return None\n"
        "    def result_schema(self, c): return {}\n"
        "    def hardware_requirements(self, c, cs): return HardwareRequirements()\n"
        "package = PluginPackage(\n"
        f"  metadata=PluginMetadata(task_type='zip_test', plugin_version='{version}',\n"
        f"    supported_versions=frozenset({{'{version}'}})),\n"
        "  master=M(), agent=object())\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", plugin_json)
        archive.writestr("main.py", main_py)
    return buffer.getvalue()


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
    package_ref = master_registry.agent_package_ref("shared_task")
    assert package_ref is not None
    assert package_ref.version == "1.0.0"


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
