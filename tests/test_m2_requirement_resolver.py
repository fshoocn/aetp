"""M2 RequirementResolver 契约测试。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from aetp_protocol.execution import (
    ExecutionRequirement,
    PluginRequirement,
    RuntimeRequirement,
    SoftwareRequirement,
)
from aetp_protocol.ids import (
    PluginId,
    SemVer,
    Sha256,
    Version,
    VersionConstraint,
    VersionRange,
)
from aetp_protocol.plugin_types import EntrypointRef, PluginPoint, PluginStatus
from aetp_protocol.plugins import PluginEntrypoints, PluginManifest, StaticRequirements

from master.domain.models import PluginVersionRecord
from master.domain.plugin_resolver import PluginResolver
from master.domain.requirement_resolver import RequirementConflict, RequirementResolver
from master.plugins.v2_registry import V2PluginRegistry

PLUGIN_ID = PluginId("org.example.executor")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _registry(tmp_path) -> V2PluginRegistry:
    archive = tmp_path / "2.0.0" / "archive.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"plugin")
    registry = V2PluginRegistry(tmp_path)
    registry.register(
        PluginVersionRecord(
            id=None,
            plugin_id=PLUGIN_ID,
            version=SemVer("2.0.0"),
            point=PluginPoint.EXECUTOR,
            status=PluginStatus.ENABLED,
            filename="executor.zip",
            archive_sha256=Sha256(hashlib.sha256(b"plugin").hexdigest()),
            manifest_sha256=Sha256("b" * 64),
            manifest=PluginManifest(
                schema_version=2,
                id=PLUGIN_ID,
                version=SemVer("2.0.0"),
                api_version=SemVer("2.0.0"),
                point=PluginPoint.EXECUTOR,
                display_name="Executor",
                entrypoints=PluginEntrypoints(
                    master=EntrypointRef("plugin:create_plugin"),
                    agent=EntrypointRef("plugin:create_plugin"),
                ),
                static_requirements=StaticRequirements(
                    runtimes=(
                        RuntimeRequirement(
                            runtime_type="python",
                            version=VersionConstraint(minimum=Version("3.11")),
                        ),
                    ),
                ),
            ),
            archive_path=str(archive),
            installed_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return registry


def test_requirement_resolver_merges_static_and_dynamic_intersection(tmp_path) -> None:
    resolver = RequirementResolver(PluginResolver(_registry(tmp_path)))

    resolved = resolver.resolve(
        PluginRequirement(
            plugin_id=PLUGIN_ID,
            version=VersionRange(minimum=SemVer("2.0.0")),
        ),
        PluginPoint.EXECUTOR,
        dynamic=ExecutionRequirement(
            executor=PluginRequirement(
                plugin_id=PLUGIN_ID,
                version=VersionRange(minimum=SemVer("1.0.0")),
            ),
            runtimes=(
                RuntimeRequirement(
                    runtime_type="python",
                    version=VersionConstraint(maximum=Version("3.12")),
                ),
            ),
            software=(SoftwareRequirement(name="CANoe"),),
        ),
    )

    runtime = resolved.requirement.runtimes[0]
    assert resolved.plugin.ref.version == SemVer("2.0.0")
    assert resolved.requirement.executor.version.exact == SemVer("2.0.0")
    assert runtime.version is not None
    assert runtime.version.minimum == Version("3.11")
    assert runtime.version.maximum == Version("3.12")
    assert resolved.requirement.software[0].name == "CANoe"


def test_requirement_resolver_rejects_empty_version_intersection(tmp_path) -> None:
    resolver = RequirementResolver(PluginResolver(_registry(tmp_path)))

    with pytest.raises(RequirementConflict, match="交集为空"):
        resolver.merge(
            PluginManifest(
                schema_version=2,
                id=PLUGIN_ID,
                version=SemVer("2.0.0"),
                api_version=SemVer("2.0.0"),
                point=PluginPoint.EXECUTOR,
                display_name="Executor",
                entrypoints=PluginEntrypoints(
                    master=EntrypointRef("plugin:create_plugin"),
                    agent=EntrypointRef("plugin:create_plugin"),
                ),
                static_requirements=StaticRequirements(
                    runtimes=(
                        RuntimeRequirement(
                            runtime_type="python",
                            version=VersionConstraint(minimum=Version("3.13")),
                        ),
                    ),
                ),
            ),
            PluginRequirement(
                plugin_id=PLUGIN_ID,
                version=VersionRange(exact=SemVer("2.0.0")),
            ),
            dynamic=ExecutionRequirement(
                executor=PluginRequirement(
                    plugin_id=PLUGIN_ID,
                    version=VersionRange(exact=SemVer("2.0.0")),
                ),
                runtimes=(
                    RuntimeRequirement(
                        runtime_type="python",
                        version=VersionConstraint(maximum=Version("3.12")),
                    ),
                ),
            ),
        )
