"""runtime/software 插件 Provider 与 Agent 能力快照集成测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aetp_protocol.capabilities import (
    LanguageCapability,
    LanguageRuntime,
    NodeCapabilities,
    NodeCapabilitySnapshot,
    RuntimeCapability,
    SoftwareCapability,
    Version,
)
from aetp_protocol.execution import SoftwareRequirement, VersionConstraint
from aetp_protocol.ids import BusinessId, PluginId, SemVer, SessionId, Sha256
from aetp_protocol.ids import (
    Version as RequirementVersion,
)
from aetp_protocol.plugin_types import EntrypointRef, PluginAvailability, PluginPoint, PluginRef
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest, StaticRequirements

from agent.application.services import capability_snapshot_service as snapshot_mod
from agent.application.services.capability_snapshot_service import AgentCapabilitySnapshotService
from agent.plugins.installer import InstalledPlugin
from agent.plugins.registry import PluginRegistry

NODE_ID = BusinessId("01J000000000000000000000C0")
SESSION_ID = SessionId("session-00000300")


class _PythonRuntimeProvider:
    provider_id = "org.vendor.python-runtime"
    runtime_type = "python"

    def __init__(self, runtime: RuntimeCapability) -> None:
        self._runtime = runtime

    def discover(self) -> tuple[RuntimeCapability, ...]:
        return (self._runtime,)


class _CanoeSoftwareProvider:
    provider_id = "org.vendor.canoe-software"
    name = "CANoe"

    def __init__(self, software: SoftwareCapability) -> None:
        self._software = software

    def discover(self) -> tuple[SoftwareCapability, ...]:
        return (self._software,)


class _BrokenRuntimeProvider:
    provider_id = "org.vendor.broken-runtime"
    runtime_type = "dotnet"

    def discover(self) -> tuple[RuntimeCapability, ...]:
        raise RuntimeError("boom")


def _base_capabilities() -> NodeCapabilities:
    return NodeCapabilities(
        language=LanguageCapability(
            runtimes=(
                LanguageRuntime(name="python", version=Version("3.12")),
                LanguageRuntime(name="java", version=Version("17.0")),
            )
        ),
    )


def _provider_python() -> RuntimeCapability:
    return RuntimeCapability(
        provider_id="org.vendor.python-runtime",
        runtime_id="python:3.11.4",
        runtime_type="python",
        version=Version("3.11.4"),
        executable_ref="C:/Tools/python-3.11/python.exe",
    )


def _provider_canoe() -> SoftwareCapability:
    return SoftwareCapability(
        provider_id="org.vendor.canoe-software",
        name="CANoe",
        version=Version("17.0"),
        properties={"license_available": True},
    )


def _service(
    registry: PluginRegistry,
    *,
    runtime_providers: tuple = (),
    software_providers: tuple = (),
) -> AgentCapabilitySnapshotService:
    return AgentCapabilitySnapshotService(
        NODE_ID,
        SESSION_ID,
        registry,
        capability_scanner=_base_capabilities,
        runtime_providers=runtime_providers,
        software_providers=software_providers,
    )


def _canoe_executor_manifest(registry: PluginRegistry, root: Path) -> PluginManifest:
    manifest = PluginManifest(
        schema_version=2,
        id=PluginId("org.example.canoe-runner"),
        version=SemVer("2.0.0"),
        api_version=SemVer("2.0.0"),
        point=PluginPoint.EXECUTOR,
        display_name="CANoe Runner",
        entrypoints=PluginEntrypoints(
            master=EntrypointRef("plugin:create_plugin"),
            agent=EntrypointRef("plugin:create_plugin"),
        ),
        static_requirements=StaticRequirements(
            software=(
                SoftwareRequirement(
                    name="CANoe",
                    version=VersionConstraint(minimum=RequirementVersion("17")),
                    license_required=True,
                ),
            ),
        ),
    )
    directory = root / "canoe-runner"
    directory.mkdir(parents=True)
    manifest_path = directory / "plugin.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")
    registry.register(
        InstalledPlugin(
            ref=PluginRef(
                plugin_id=manifest.id,
                version=manifest.version,
                archive_sha256=Sha256(hashlib.sha256(b"canoe").hexdigest()),
            ),
            manifest_path=manifest_path,
            install_path=directory,
        )
    )
    return manifest


def test_snapshot_merges_runtime_provider_and_suppresses_owned_base_type(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = PluginRegistry()
    service = _service(
        registry,
        runtime_providers=(_PythonRuntimeProvider(_provider_python()),),
    )

    snapshot = service.build_snapshot()
    runtimes = list(snapshot.runtimes)

    # python 由 Provider 拥有：基础 agent.discovery 的 python 被抑制
    assert [(r.runtime_type, r.provider_id, r.version.root) for r in runtimes] == [
        ("python", "org.vendor.python-runtime", "3.11.4"),
        ("java", "agent.discovery", "17.0"),
    ]


def test_snapshot_merges_software_provider_and_suppresses_owned_base_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        snapshot_mod,
        "discover_software",
        lambda: (
            SoftwareCapability(
                provider_id="agent.discovery",
                name="CANoe",
                version=Version("17.0"),
            ),
            SoftwareCapability(
                provider_id="agent.discovery",
                name="Vector Driver",
                version=Version("20.1"),
            ),
        ),
    )
    registry = PluginRegistry()
    service = _service(
        registry,
        software_providers=(_CanoeSoftwareProvider(_provider_canoe()),),
    )

    snapshot = service.build_snapshot()
    software = list(snapshot.software)

    # CANoe 由 Provider 拥有：基础 agent.discovery 的 CANoe 被抑制；
    # Vector Driver 未被覆盖，仍由基础探测补充。
    assert [(s.name, s.provider_id, s.properties.get("license_available")) for s in software] == [
        ("CANoe", "org.vendor.canoe-software", True),
        ("Vector Driver", "agent.discovery", None),
    ]


def test_snapshot_without_providers_keeps_base_discovery(tmp_path: Path) -> None:
    registry = PluginRegistry()
    service = _service(registry)

    snapshot: NodeCapabilitySnapshot = service.build_snapshot()

    assert {r.runtime_type for r in snapshot.runtimes} == {"python", "java"}
    assert all(r.provider_id == "agent.discovery" for r in snapshot.runtimes)


def test_snapshot_broken_provider_is_skipped_without_crash(tmp_path: Path) -> None:
    registry = PluginRegistry()
    service = _service(registry, runtime_providers=(_BrokenRuntimeProvider(),))

    snapshot = service.build_snapshot()

    # dotnet Provider 失败被跳过（发现为空）；基础 python/java 不受影响
    assert [(r.runtime_type, r.provider_id) for r in snapshot.runtimes] == [
        ("python", "agent.discovery"),
        ("java", "agent.discovery"),
    ]
    assert service.build_snapshot().revision == 2


def test_plugin_requiring_canoe_license_becomes_available_via_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = PluginRegistry()
    _canoe_executor_manifest(registry, tmp_path)
    service = _service(
        registry,
        software_providers=(_CanoeSoftwareProvider(_provider_canoe()),),
    )

    snapshot = service.build_snapshot()

    item = next(
        item
        for item in snapshot.plugin_inventory
        if item.plugin_id.root == "org.example.canoe-runner"
    )
    assert item.availability is PluginAvailability.AVAILABLE
    assert item.unavailable_reasons == ()
    assert [e.plugin_id.root for e in snapshot.executors] == ["org.example.canoe-runner"]
