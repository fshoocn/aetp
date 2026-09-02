"""V2 NodeMatcher：根据能力快照评估 ExecutionRequirement。"""

from __future__ import annotations

from dataclasses import dataclass

from aetp_protocol.capabilities import (
    NodeCapabilitySnapshot,
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
    ResourceRequirement,
    RuntimeRequirement,
    SoftwareRequirement,
)
from aetp_protocol.ids import BusinessId, VersionConstraint
from aetp_protocol.ids import Version as ConstraintVersion
from aetp_protocol.plugin_types import PluginAvailability, PluginPoint

from master.domain.plugin_resolver import semver_satisfies


@dataclass(frozen=True)
class NodeCapabilityCandidate:
    """参与匹配的节点快照及 Master 节点投影状态。"""

    snapshot: NodeCapabilitySnapshot
    online: bool = True
    enabled: bool = True
    maintenance_locked: bool = False


@dataclass(frozen=True)
class NodeMatch:
    """单节点匹配结果和可展示的稳定原因码。"""

    node_id: BusinessId
    matched: bool
    failures: tuple[ErrorCode, ...] = ()


class NodeMatcher:
    """只读取快照匹配需求，不分配节点、不申请 Lease。"""

    def evaluate(
        self,
        candidate: NodeCapabilityCandidate,
        requirement: ExecutionRequirement,
    ) -> NodeMatch:
        failures: list[ErrorCode] = []
        if not candidate.enabled:
            failures.append(ErrorCode("NODE_CAPABILITY_MISMATCH"))
        if not candidate.online:
            failures.append(ErrorCode("AGENT_OFFLINE"))
        if candidate.maintenance_locked:
            failures.append(ErrorCode("AGENT_MAINTENANCE"))
        failures.extend(self._match_executor(candidate.snapshot, requirement))
        failures.extend(self._match_runtimes(candidate.snapshot.runtimes, requirement.runtimes))
        failures.extend(self._match_software(candidate.snapshot.software, requirement.software))
        failures.extend(self._match_resources(candidate.snapshot.resources, requirement.resources))
        actual_tags = set(candidate.snapshot.tags)
        if any(tag not in actual_tags for tag in requirement.required_tags):
            failures.append(ErrorCode("NODE_CAPABILITY_MISMATCH"))
        return NodeMatch(
            node_id=candidate.snapshot.node_id,
            matched=not failures,
            failures=_unique_codes(tuple(failures)),
        )

    def match(
        self,
        candidates: tuple[NodeCapabilityCandidate, ...],
        requirement: ExecutionRequirement,
    ) -> tuple[NodeMatch, ...]:
        """返回按输入顺序评估的结果，调用方可按自己的策略排序。"""
        return tuple(self.evaluate(candidate, requirement) for candidate in candidates)

    @staticmethod
    def _match_executor(
        snapshot: NodeCapabilitySnapshot,
        requirement: ExecutionRequirement,
    ) -> tuple[ErrorCode, ...]:
        plugin_id = requirement.executor.plugin_id
        versions = tuple(
            item
            for item in snapshot.plugin_inventory
            if item.plugin_id == plugin_id and item.point is PluginPoint.EXECUTOR
        )
        if not versions:
            return (ErrorCode("PLUGIN_VERSION_UNAVAILABLE"),)
        matching = tuple(
            item for item in versions if semver_satisfies(item.version, requirement.executor.version)
        )
        if not matching:
            return (ErrorCode("PLUGIN_VERSION_UNAVAILABLE"),)
        unavailable = tuple(
            reason
            for item in matching
            if item.availability is not PluginAvailability.AVAILABLE
            for reason in item.unavailable_reasons
        )
        if unavailable:
            return _unique_codes(unavailable)
        if not any(
            executor.plugin_id == plugin_id and executor.version == item.version
            for item in matching
            for executor in snapshot.executors
        ):
            return (ErrorCode("PLUGIN_VERSION_UNAVAILABLE"),)
        return ()

    @staticmethod
    def _match_runtimes(
        runtimes: tuple[RuntimeCapability, ...],
        requirements: tuple[RuntimeRequirement, ...],
    ) -> tuple[ErrorCode, ...]:
        failures: list[ErrorCode] = []
        for requirement in requirements:
            matching = tuple(item for item in runtimes if item.runtime_type == requirement.runtime_type)
            if not matching or (
                requirement.version is not None
                and not any(_version_satisfies(item.version, requirement.version) for item in matching)
            ):
                failures.append(ErrorCode("RUNTIME_NOT_FOUND"))
        return tuple(failures)

    @staticmethod
    def _match_software(
        software: tuple[SoftwareCapability, ...],
        requirements: tuple[SoftwareRequirement, ...],
    ) -> tuple[ErrorCode, ...]:
        failures: list[ErrorCode] = []
        for requirement in requirements:
            matching = tuple(item for item in software if item.name == requirement.name)
            if not matching:
                failures.append(ErrorCode("SOFTWARE_NOT_FOUND"))
                continue
            if requirement.version is not None and not any(
                _version_satisfies(item.version, requirement.version) for item in matching
            ):
                failures.append(ErrorCode("SOFTWARE_VERSION_MISMATCH"))
            if requirement.license_required and not any(
                item.properties.get("license_available") is True for item in matching
            ):
                failures.append(ErrorCode("SOFTWARE_NOT_FOUND"))
        return tuple(failures)

    @staticmethod
    def _match_resources(
        resources: tuple[ResourceCapability, ...],
        requirements: tuple[ResourceRequirement, ...],
    ) -> tuple[ErrorCode, ...]:
        failures: list[ErrorCode] = []
        slots = tuple(
            requirement
            for requirement in requirements
            for _ in range(requirement.quantity)
        )
        matched_resources: dict[int, int] = {}

        def assign(slot_index: int, visited: set[int]) -> bool:
            requirement = slots[slot_index]
            for resource_index, resource in enumerate(resources):
                if resource_index in visited or not _resource_matches(resource, requirement):
                    continue
                visited.add(resource_index)
                previous_slot = matched_resources.get(resource_index)
                if previous_slot is None or assign(previous_slot, visited):
                    matched_resources[resource_index] = slot_index
                    return True
            return False

        for slot_index in range(len(slots)):
            if not assign(slot_index, set()):
                failures.append(ErrorCode("RESOURCE_UNAVAILABLE"))
                break
        return tuple(failures)


def _resource_matches(resource: ResourceCapability, requirement: ResourceRequirement) -> bool:
    if resource.health is not ResourceHealth.READY:
        return False
    if resource.resource_type != requirement.resource_type:
        return False
    if requirement.vendor is not None and resource.vendor != requirement.vendor:
        return False
    if requirement.model is not None and resource.model != requirement.model:
        return False
    if any(resource.labels.get(key) != value for key, value in requirement.required_labels.items()):
        return False
    return all(resource.properties.get(key) == value for key, value in requirement.properties.items())


def _version_satisfies(actual: CapabilityVersion, requirement: VersionConstraint) -> bool:
    actual_parts = _version_parts(actual)
    if requirement.exact is not None and actual_parts != _version_parts(requirement.exact):
        return False
    if requirement.minimum is not None and actual_parts < _version_parts(requirement.minimum):
        return False
    return requirement.maximum is None or actual_parts <= _version_parts(requirement.maximum)


def _version_parts(version: CapabilityVersion | ConstraintVersion) -> tuple[int, ...]:
    return tuple(int(part) for part in version.root.removeprefix("v").split("."))


def _unique_codes(codes: tuple[ErrorCode, ...]) -> tuple[ErrorCode, ...]:
    result: list[ErrorCode] = []
    for code in codes:
        if code not in result:
            result.append(code)
    return tuple(result)


__all__ = ["NodeCapabilityCandidate", "NodeMatch", "NodeMatcher"]
