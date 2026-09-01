"""M2 PluginResolver 和 NodeMatcher 契约测试。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from aetp_protocol.capabilities import (
    AgentMaintenanceState,
    ExecutorCapability,
    NodeCapabilitySnapshot,
    PluginInventoryItem,
    ResourceCapability,
    ResourceHealth,
    RuntimeCapability,
    SoftwareCapability,
)
from aetp_protocol.capabilities import (
    Version as CapabilityVersion,
)
from aetp_protocol.errors import ErrorCode
from aetp_protocol.execution import (
    ExecutionRequirement,
    PluginRequirement,
    ResourceRequirement,
    RuntimeRequirement,
    SoftwareRequirement,
)
from aetp_protocol.ids import (
    BusinessId,
    CapabilityName,
    PluginId,
    SemVer,
    SessionId,
    Sha256,
    VersionConstraint,
    VersionRange,
)
from aetp_protocol.ids import (
    Version as RequirementVersion,
)
from aetp_protocol.plugin_types import EntrypointRef, PluginAvailability, PluginPoint, PluginStatus
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest

from master.domain.models import PluginVersionRecord
from master.domain.node_matcher import NodeCapabilityCandidate, NodeMatcher
from master.domain.plugin_resolver import PluginResolver, PluginVersionUnavailable
from master.plugins.v2_registry import V2PluginRegistry

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
PLUGIN_ID = PluginId("org.example.executor")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _manifest(version: str = "2.0.0") -> PluginManifest:
    return PluginManifest(
        schema_version=2,
        id=PLUGIN_ID,
        version=SemVer(version),
        api_version=SemVer("2.0.0"),
        point=PluginPoint.EXECUTOR,
        display_name="Example Executor",
        entrypoints=PluginEntrypoints(
            master=EntrypointRef("plugin:create_plugin"),
            agent=EntrypointRef("plugin:create_plugin"),
        ),
        capabilities=(CapabilityName("test.execute"),),
    )


def _record(tmp_path, version: str) -> PluginVersionRecord:
    archive = tmp_path / version / "archive.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(version.encode("ascii"))
    return PluginVersionRecord(
        id=None,
        plugin_id=PLUGIN_ID,
        version=SemVer(version),
        point=PluginPoint.EXECUTOR,
        status=PluginStatus.ENABLED,
        filename=f"executor-{version}.zip",
        archive_sha256=Sha256(hashlib.sha256(version.encode("ascii")).hexdigest()),
        manifest_sha256=Sha256("b" * 64),
        manifest=_manifest(version),
        archive_path=str(archive),
        installed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def test_plugin_resolver_selects_highest_enabled_version(tmp_path) -> None:
    registry = V2PluginRegistry(tmp_path)
    registry.register(_record(tmp_path, "2.0.0"))
    registry.register(_record(tmp_path, "2.10.0"))
    resolver = PluginResolver(registry)

    resolved = resolver.resolve(
        PluginRequirement(
            plugin_id=PLUGIN_ID,
            version=VersionRange(minimum=SemVer("2.0.0")),
        ),
        PluginPoint.EXECUTOR,
    )

    assert resolved.ref.version == SemVer("2.10.0")
    assert "2.10.0" in resolved.reason


def test_plugin_resolver_does_not_silently_install_or_downgrade(tmp_path) -> None:
    registry = V2PluginRegistry(tmp_path)
    registry.register(_record(tmp_path, "2.0.0"))
    resolver = PluginResolver(registry)

    with pytest.raises(PluginVersionUnavailable):
        resolver.resolve(
            PluginRequirement(
                plugin_id=PLUGIN_ID,
                version=VersionRange(exact=SemVer("3.0.0")),
            ),
            PluginPoint.EXECUTOR,
        )


def test_v2_registry_rejects_same_version_different_archive(tmp_path) -> None:
    registry = V2PluginRegistry(tmp_path)
    original = _record(tmp_path, "2.0.0")
    registry.register(original)
    conflicting = original
    alternate_archive = tmp_path / "alternate" / "archive.zip"
    alternate_archive.parent.mkdir()
    alternate_content = b"another-plugin-archive"
    alternate_archive.write_bytes(alternate_content)
    conflicting = replace(
        conflicting,
        archive_path=str(alternate_archive),
        archive_sha256=Sha256(hashlib.sha256(alternate_content).hexdigest()),
    )

    with pytest.raises(ValueError, match="摘要冲突"):
        registry.register(conflicting)


def _snapshot(
    *,
    inventory: tuple[PluginInventoryItem, ...] = (),
    executors: tuple[ExecutorCapability, ...] = (),
    runtimes: tuple[RuntimeCapability, ...] = (),
    software: tuple[SoftwareCapability, ...] = (),
    resources: tuple[ResourceCapability, ...] = (),
    online: bool = True,
) -> NodeCapabilityCandidate:
    return NodeCapabilityCandidate(
        snapshot=NodeCapabilitySnapshot(
            schema_version=2,
            node_id=NODE_ID,
            session_id=SESSION_ID,
            revision=1,
            reported_at=NOW,
            maintenance_state=AgentMaintenanceState.IDLE,
            plugin_inventory=inventory,
            executors=executors,
            runtimes=runtimes,
            software=software,
            resources=resources,
        ),
        online=online,
    )


def _inventory(
    *,
    availability: PluginAvailability = PluginAvailability.AVAILABLE,
    reasons: tuple[ErrorCode, ...] = (),
    version: str = "2.0.0",
) -> PluginInventoryItem:
    return PluginInventoryItem(
        plugin_id=PLUGIN_ID,
        point=PluginPoint.EXECUTOR,
        version=SemVer(version),
        archive_sha256=Sha256("a" * 64),
        availability=availability,
        unavailable_reasons=reasons,
        checked_at=NOW,
    )


def _requirement(
    *,
    version: VersionRange | None = None,
    runtimes: tuple[RuntimeRequirement, ...] = (),
    software: tuple[SoftwareRequirement, ...] = (),
    resources: tuple[ResourceRequirement, ...] = (),
) -> ExecutionRequirement:
    return ExecutionRequirement(
        executor=PluginRequirement(
            plugin_id=PLUGIN_ID,
            version=version or VersionRange(exact=SemVer("2.0.0")),
        ),
        runtimes=runtimes,
        software=software,
        resources=resources,
    )


def test_node_matcher_reports_missing_canoe_with_stable_reason() -> None:
    matcher = NodeMatcher()
    candidate = _snapshot(
        inventory=(_inventory(availability=PluginAvailability.BLOCKED, reasons=(ErrorCode("SOFTWARE_NOT_FOUND"),)),),
    )

    result = matcher.evaluate(
        candidate,
        _requirement(
            software=(SoftwareRequirement(name="CANoe"),),
        ),
    )

    assert result.matched is False
    assert result.failures == (ErrorCode("SOFTWARE_NOT_FOUND"),)


def test_node_matcher_requires_available_executor_runtime_software_and_resource() -> None:
    matcher = NodeMatcher()
    candidate = _snapshot(
        inventory=(_inventory(),),
        executors=(
            ExecutorCapability(
                plugin_id=PLUGIN_ID,
                version=SemVer("2.0.0"),
                capabilities=(CapabilityName("test.execute"),),
            ),
        ),
        runtimes=(
            RuntimeCapability(
                provider_id="python",
                runtime_id="python-312",
                runtime_type="python",
                version=CapabilityVersion("3.12"),
            ),
        ),
        software=(
            SoftwareCapability(
                provider_id="vector",
                name="CANoe",
                version=CapabilityVersion("17"),
                properties={"license_available": True},
            ),
        ),
        resources=(
            ResourceCapability(
                resource_id=BusinessId("01J00000000000000000000001"),
                provider_id="vector",
                resource_type="can",
                health=ResourceHealth.READY,
            ),
        ),
    )

    result = matcher.evaluate(
        candidate,
        _requirement(
            runtimes=(
                RuntimeRequirement(
                    runtime_type="python",
                    version=VersionConstraint(minimum=RequirementVersion("3.0")),
                ),
            ),
            software=(
                SoftwareRequirement(name="CANoe", version=VersionConstraint(minimum=RequirementVersion("17"))),
            ),
            resources=(ResourceRequirement(resource_type="can"),),
        ),
    )

    assert result.matched is True
    assert result.failures == ()


def test_node_matcher_reports_plugin_version_and_resource_failures() -> None:
    matcher = NodeMatcher()
    candidate = _snapshot(inventory=(_inventory(version="1.0.0"),))

    result = matcher.evaluate(
        candidate,
        _requirement(
            version=VersionRange(exact=SemVer("2.0.0")),
            resources=(ResourceRequirement(resource_type="can", quantity=2),),
        ),
    )

    assert result.matched is False
    assert ErrorCode("PLUGIN_VERSION_UNAVAILABLE") in result.failures
    assert ErrorCode("RESOURCE_UNAVAILABLE") in result.failures


def test_node_matcher_does_not_reuse_one_resource_for_two_requirements() -> None:
    candidate = _snapshot(
        inventory=(_inventory(),),
        executors=(
            ExecutorCapability(
                plugin_id=PLUGIN_ID,
                version=SemVer("2.0.0"),
                capabilities=(CapabilityName("test.execute"),),
            ),
        ),
        resources=(
            ResourceCapability(
                resource_id=BusinessId("01J00000000000000000000001"),
                provider_id="vector",
                resource_type="can",
                health=ResourceHealth.READY,
            ),
        ),
    )

    result = NodeMatcher().evaluate(
        candidate,
        _requirement(
            resources=(
                ResourceRequirement(resource_type="can"),
                ResourceRequirement(resource_type="can"),
            ),
        ),
    )

    assert result.matched is False
    assert result.failures == (ErrorCode("RESOURCE_UNAVAILABLE"),)
