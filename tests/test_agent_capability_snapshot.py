"""Agent 能力快照和插件可用性测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aetp_protocol.capabilities import (
    LanguageCapability,
    LanguageRuntime,
    NodeCapabilities,
    OperatingSystem,
    SystemCapability,
    Version,
)
from aetp_protocol.execution import RuntimeRequirement, SoftwareRequirement
from aetp_protocol.ids import (
    BusinessId,
    PluginId,
    SemVer,
    SessionId,
    Sha256,
    VersionConstraint,
)
from aetp_protocol.ids import (
    Version as RequirementVersion,
)
from aetp_protocol.plugin_types import EntrypointRef, PluginAvailability, PluginPoint, PluginRef
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest, StaticRequirements

from agent.application.services.capability_snapshot_service import (
    AgentCapabilitySnapshotService,
    CapabilityRevisionCache,
    evaluate_plugin_availability,
)
from agent.plugins.installer import InstalledPlugin
from agent.plugins.registry import PluginRegistry

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
NEW_SESSION_ID = SessionId("session-00000002")


def _manifest(
    plugin_id: str,
    *,
    requirements: StaticRequirements | None = None,
) -> PluginManifest:
    return PluginManifest(
        schema_version=2,
        id=PluginId(plugin_id),
        version=SemVer("2.0.0"),
        api_version=SemVer("2.0.0"),
        point=PluginPoint.EXECUTOR,
        display_name=plugin_id,
        entrypoints=PluginEntrypoints(
            master=EntrypointRef("plugin:create_plugin"),
            agent=EntrypointRef("plugin:create_plugin"),
        ),
        static_requirements=requirements or StaticRequirements(),
    )


def _register(registry: PluginRegistry, root: Path, manifest: PluginManifest, suffix: str) -> None:
    directory = root / suffix
    directory.mkdir(parents=True)
    manifest_path = directory / "plugin.json"
    manifest_path.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")
    registry.register(
        InstalledPlugin(
            ref=PluginRef(
                plugin_id=manifest.id,
                version=manifest.version,
                archive_sha256=Sha256(hashlib.sha256(suffix.encode("utf-8")).hexdigest()),
            ),
            manifest_path=manifest_path,
            install_path=directory,
        )
    )


def _base_capabilities() -> NodeCapabilities:
    return NodeCapabilities(
        language=LanguageCapability(
            runtimes=(LanguageRuntime(name="python", version=Version("3.12")),)
        ),
        system=SystemCapability(
            operating_system=OperatingSystem(name="windows", version=Version("10.0")),
            memory_mb=16_384,
            cpu_cores=8,
        ),
    )


def test_plugin_availability_reports_missing_canoe_as_blocked() -> None:
    manifest = _manifest(
        "org.example.canoe",
        requirements=StaticRequirements(
            software=(
                SoftwareRequirement(
                    name="CANoe",
                    version=VersionConstraint(minimum=RequirementVersion("17")),
                ),
            ),
        ),
    )

    evaluation = evaluate_plugin_availability(manifest)

    assert evaluation.availability is PluginAvailability.BLOCKED
    assert evaluation.unavailable_reasons[0].root == "SOFTWARE_NOT_FOUND"


def test_snapshot_exposes_only_available_executors_and_monotonic_revision(tmp_path) -> None:
    registry = PluginRegistry()
    available = _manifest("org.example.available")
    blocked = _manifest(
        "org.example.python",
        requirements=StaticRequirements(
            runtimes=(
                RuntimeRequirement(
                    runtime_type="python",
                    version=VersionConstraint(minimum=RequirementVersion("3.13")),
                ),
            ),
        ),
    )
    _register(registry, tmp_path, available, "available")
    _register(registry, tmp_path, blocked, "blocked")
    revision_cache = CapabilityRevisionCache()
    service = AgentCapabilitySnapshotService(
        NODE_ID,
        SESSION_ID,
        registry,
        capability_scanner=_base_capabilities,
        revision_cache=revision_cache,
    )

    first = service.build_snapshot()
    second = service.build_snapshot()

    assert first.revision == 1
    assert second.revision == 2
    assert [item.plugin_id.root for item in first.executors] == ["org.example.available"]
    blocked_item = next(item for item in first.plugin_inventory if item.plugin_id.root == "org.example.python")
    assert blocked_item.availability is PluginAvailability.BLOCKED
    assert blocked_item.unavailable_reasons[0].root == "RUNTIME_NOT_FOUND"
    assert first.resources == second.resources

    new_session_service = AgentCapabilitySnapshotService(
        NODE_ID,
        NEW_SESSION_ID,
        registry,
        capability_scanner=_base_capabilities,
        revision_cache=revision_cache,
    )
    assert new_session_service.build_snapshot().revision == 1
