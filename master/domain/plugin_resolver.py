""" 插件版本解析策略。"""

from __future__ import annotations

from dataclasses import dataclass

from aetp_protocol.execution import PluginRequirement
from aetp_protocol.ids import SemVer, VersionRange
from aetp_protocol.plugin_types import PluginPoint, PluginRef
from aetp_protocol.plugins import PluginManifest

from master.plugins.registry import PluginRegistry


@dataclass(frozen=True)
class ResolvedPlugin:
    """需求解析后的精确插件引用和可审计选择理由。"""

    ref: PluginRef
    reason: str


class PluginVersionUnavailable(ValueError):
    """没有已启用版本满足插件需求。"""

    code = "PLUGIN_VERSION_UNAVAILABLE"


class PluginResolver:
    """只从 Master  Registry 解析已启用插件，不自动安装或降级。"""

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def resolve(self, requirement: PluginRequirement, point: PluginPoint) -> ResolvedPlugin:
        candidates = [
            record
            for record in self._registry.list(point)
            if record.plugin_id == requirement.plugin_id
            and semver_satisfies(record.version, requirement.version)
        ]
        if not candidates:
            raise PluginVersionUnavailable(
                f"没有已启用插件满足需求: {requirement.plugin_id.root} ({point.value})"
            )
        candidates.sort(key=lambda record: _semver_key(record.version), reverse=True)
        selected = candidates[0]
        return ResolvedPlugin(
            ref=PluginRef(
                plugin_id=selected.plugin_id,
                version=selected.version,
                archive_sha256=selected.archive_sha256,
            ),
            reason=(
                f"从 {len(candidates)} 个已启用版本中按 SemVer 降序选择 "
                f"{selected.version.root}"
            ),
        )

    def manifest_for(self, ref: PluginRef, point: PluginPoint) -> PluginManifest:
        """返回已解析插件引用对应的已启用 Manifest。"""
        record = self._registry.get(ref.plugin_id, ref.version, point)
        if record is None:
            raise PluginVersionUnavailable(
                f"插件引用不存在或未启用: {ref.plugin_id.root}@{ref.version.root}"
            )
        return record.manifest


def semver_satisfies(version: SemVer, requirement: VersionRange) -> bool:
    actual = _semver_key(version)
    if requirement.exact is not None and actual != _semver_key(requirement.exact):
        return False
    if requirement.minimum is not None and actual < _semver_key(requirement.minimum):
        return False
    return requirement.maximum is None or actual <= _semver_key(requirement.maximum)


def intersect_semver_ranges(first: VersionRange, second: VersionRange) -> VersionRange:
    """返回两个 SemVer 范围的交集，空交集时抛出 ValueError。"""
    exact = first.exact or second.exact
    if (
        first.exact is not None
        and second.exact is not None
        and _semver_key(first.exact) != _semver_key(second.exact)
    ):
        raise ValueError("SemVer 约束交集为空")
    minimum = _maximum(first.minimum, second.minimum)
    maximum = _minimum(first.maximum, second.maximum)
    if exact is not None and (
        (minimum is not None and _semver_key(exact) < _semver_key(minimum))
        or (maximum is not None and _semver_key(exact) > _semver_key(maximum))
    ):
        raise ValueError("SemVer 约束交集为空")
    if minimum is not None and maximum is not None and _semver_key(minimum) > _semver_key(maximum):
        raise ValueError("SemVer 约束交集为空")
    return VersionRange(exact=exact, minimum=minimum, maximum=maximum)


def _maximum(first: SemVer | None, second: SemVer | None) -> SemVer | None:
    if first is None:
        return second
    if second is None:
        return first
    return first if _semver_key(first) >= _semver_key(second) else second


def _minimum(first: SemVer | None, second: SemVer | None) -> SemVer | None:
    if first is None:
        return second
    if second is None:
        return first
    return first if _semver_key(first) <= _semver_key(second) else second


def _semver_key(version: SemVer) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    value = version.root.split("+", 1)[0]
    core, _, prerelease = value.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    if not prerelease:
        return major, minor, patch, 1, ()
    identifiers: list[tuple[int, int | str]] = []
    for identifier in prerelease.split("."):
        if identifier.isdigit():
            identifiers.append((0, int(identifier)))
        else:
            identifiers.append((1, identifier))
    return major, minor, patch, 0, tuple(identifiers)
