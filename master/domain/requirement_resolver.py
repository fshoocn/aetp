""" ExecutionRequirement 合并和插件版本解析。"""

from __future__ import annotations

from dataclasses import dataclass

from aetp_protocol.execution import (
    ExecutionRequirement,
    PluginRequirement,
    RuntimeRequirement,
    SoftwareRequirement,
)
from aetp_protocol.ids import Version, VersionConstraint, VersionRange
from aetp_protocol.plugin_types import PluginPoint
from aetp_protocol.plugins import PluginManifest

from master.domain.plugin_resolver import (
    PluginResolver,
    ResolvedPlugin,
    intersect_semver_ranges,
)


class RequirementConflict(ValueError):
    """静态和动态需求交集为空。"""

    code = "REQUIREMENT_CONFLICT"


@dataclass(frozen=True)
class ResolvedRequirement:
    """需求解析后的精确插件引用、合并需求和选择理由。"""

    plugin: ResolvedPlugin
    requirement: ExecutionRequirement
    reason: str


class RequirementResolver:
    """合并 Manifest 与插件动态需求，并解析已启用插件版本。"""

    def __init__(self, plugin_resolver: PluginResolver) -> None:
        self._plugin_resolver = plugin_resolver

    def resolve(
        self,
        plugin_requirement: PluginRequirement,
        point: PluginPoint,
        *,
        dynamic: ExecutionRequirement | None = None,
    ) -> ResolvedRequirement:
        effective_requirement = plugin_requirement
        if dynamic is not None:
            if dynamic.executor.plugin_id != plugin_requirement.plugin_id:
                raise RequirementConflict("动态需求插件 ID 与插件需求不一致")
            try:
                effective_requirement = PluginRequirement(
                    plugin_id=plugin_requirement.plugin_id,
                    version=intersect_semver_ranges(
                        plugin_requirement.version,
                        dynamic.executor.version,
                    ),
                )
            except ValueError as exc:
                raise RequirementConflict("executor 版本约束交集为空") from exc
        plugin = self._plugin_resolver.resolve(effective_requirement, point)
        manifest = self._plugin_resolver.manifest_for(plugin.ref, point)
        merged = self.merge(manifest, effective_requirement, dynamic=dynamic)
        exact_executor = PluginRequirement(
            plugin_id=plugin.ref.plugin_id,
            version=VersionRange(exact=plugin.ref.version),
        )
        requirement = ExecutionRequirement(
            executor=exact_executor,
            runtimes=merged.runtimes,
            software=merged.software,
            resources=merged.resources,
            required_tags=merged.required_tags,
        )
        return ResolvedRequirement(
            plugin=plugin,
            requirement=requirement,
            reason=f"{plugin.reason}；静态和动态需求已合并",
        )

    def merge(
        self,
        manifest: PluginManifest,
        plugin_requirement: PluginRequirement,
        *,
        dynamic: ExecutionRequirement | None = None,
    ) -> ExecutionRequirement:
        """合并已选 Manifest 的静态需求和插件产生的动态需求。"""
        if plugin_requirement.plugin_id != manifest.id:
            raise RequirementConflict("插件需求 ID 与 Manifest 不一致")
        dynamic = dynamic or ExecutionRequirement(executor=plugin_requirement)
        if dynamic.executor.plugin_id != manifest.id:
            raise RequirementConflict("动态需求插件 ID 与 Manifest 不一致")
        runtimes = self._merge_runtimes(manifest.static_requirements.runtimes, dynamic.runtimes)
        software = self._merge_software(manifest.static_requirements.software, dynamic.software)
        return ExecutionRequirement(
            executor=plugin_requirement,
            runtimes=runtimes,
            software=software,
            resources=manifest.static_requirements.resources + dynamic.resources,
            required_tags=tuple(dict.fromkeys(dynamic.required_tags)),
        )

    @staticmethod
    def _merge_runtimes(
        static: tuple[RuntimeRequirement, ...],
        dynamic: tuple[RuntimeRequirement, ...],
    ) -> tuple[RuntimeRequirement, ...]:
        by_type: dict[str, VersionConstraint | None] = {}
        for requirement in static + dynamic:
            previous = by_type.get(requirement.runtime_type)
            by_type[requirement.runtime_type] = _intersect_constraints(
                previous,
                requirement.version,
                f"runtime {requirement.runtime_type}",
            )
        return tuple(
            RuntimeRequirement(runtime_type=runtime_type, version=version)
            for runtime_type, version in by_type.items()
        )

    @staticmethod
    def _merge_software(
        static: tuple[SoftwareRequirement, ...],
        dynamic: tuple[SoftwareRequirement, ...],
    ) -> tuple[SoftwareRequirement, ...]:
        by_name: dict[str, tuple[VersionConstraint | None, bool]] = {}
        for requirement in static + dynamic:
            previous = by_name.get(requirement.name)
            previous_version = previous[0] if previous is not None else None
            previous_license = previous[1] if previous is not None else False
            by_name[requirement.name] = (
                _intersect_constraints(
                    previous_version,
                    requirement.version,
                    f"software {requirement.name}",
                ),
                previous_license or requirement.license_required,
            )
        return tuple(
            SoftwareRequirement(
                name=name,
                version=version,
                license_required=license_required,
            )
            for name, (version, license_required) in by_name.items()
        )

def _intersect_constraints(
    first: VersionConstraint | None,
    second: VersionConstraint | None,
    label: str,
) -> VersionConstraint | None:
    if first is None:
        return second
    if second is None:
        return first
    exact = _same_or_one(first.exact, second.exact)
    minimum = _maximum(first.minimum, second.minimum)
    maximum = _minimum(first.maximum, second.maximum)
    if exact is not None and not _within(exact, minimum, maximum):
        raise RequirementConflict(f"{label} 版本约束交集为空")
    if minimum is not None and maximum is not None and _version_key(minimum) > _version_key(maximum):
        raise RequirementConflict(f"{label} 版本约束交集为空")
    return VersionConstraint(exact=exact, minimum=minimum, maximum=maximum)


def _same_or_one(first: Version | None, second: Version | None) -> Version | None:
    if first is not None and second is not None and _version_key(first) != _version_key(second):
        raise RequirementConflict("exact 版本约束不一致")
    return first or second


def _maximum(first: Version | None, second: Version | None) -> Version | None:
    if first is None:
        return second
    if second is None:
        return first
    return first if _version_key(first) >= _version_key(second) else second


def _minimum(first: Version | None, second: Version | None) -> Version | None:
    if first is None:
        return second
    if second is None:
        return first
    return first if _version_key(first) <= _version_key(second) else second


def _within(value: Version, minimum: Version | None, maximum: Version | None) -> bool:
    key = _version_key(value)
    return (minimum is None or key >= _version_key(minimum)) and (
        maximum is None or key <= _version_key(maximum)
    )


def _version_key(version: Version) -> tuple[int, ...]:
    return tuple(int(part) for part in version.root.removeprefix("v").split("."))
