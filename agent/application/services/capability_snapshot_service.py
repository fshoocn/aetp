"""Agent  能力快照和插件可用性计算。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aetp_protocol.capabilities import (
    AgentMaintenanceState,
    ExecutorCapability,
    NodeCapabilities,
    NodeCapabilitySnapshot,
    PluginInventoryItem,
    ResourceCapability,
    ResourceHealth,
    RuntimeCapability,
    SoftwareCapability,
)
from aetp_protocol.capabilities import Version as CapabilityVersion
from aetp_protocol.discovery import (
    RuntimeDiscoveryError,
    RuntimeProvider,
    SoftwareDiscoveryError,
    SoftwareProvider,
)
from aetp_protocol.errors import ErrorCode
from aetp_protocol.execution import ResourceRequirement, RuntimeRequirement
from aetp_protocol.ids import BusinessId, SessionId, Version, VersionConstraint, stable_id
from aetp_protocol.plugin_types import PluginAvailability, PluginPoint
from aetp_protocol.plugins import PluginManifest

from agent.application.services.capability_loader import scan_base_capabilities
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.application.services.software_discovery import discover_software
from agent.plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginAvailabilityEvaluation:
    """单个插件版本的可用性和稳定原因。"""

    availability: PluginAvailability
    unavailable_reasons: tuple[ErrorCode, ...] = ()


class CapabilityRevisionCache:
    """按节点和 Agent session 维护单调递增的快照 revision。"""

    def __init__(self) -> None:
        self._state: dict[str, tuple[str, int]] = {}

    def next(self, node_id: BusinessId, session_id: SessionId) -> int:
        key = node_id.root
        previous = self._state.get(key)
        revision = 1 if previous is None or previous[0] != session_id.root else previous[1] + 1
        self._state[key] = (session_id.root, revision)
        return revision

    def reset(self, node_id: BusinessId, session_id: SessionId) -> None:
        self._state[node_id.root] = (session_id.root, 0)


def evaluate_plugin_availability(
    manifest: PluginManifest,
    *,
    runtimes: tuple[RuntimeCapability, ...] = (),
    software: tuple[SoftwareCapability, ...] = (),
    resources: tuple[ResourceCapability, ...] = (),
    health_errors: tuple[ErrorCode, ...] = (),
) -> PluginAvailabilityEvaluation:
    """按 Manifest 静态需求计算插件库存状态。"""
    if health_errors:
        return PluginAvailabilityEvaluation(PluginAvailability.ERROR, _unique_codes(health_errors))

    reasons: list[ErrorCode] = []
    for requirement in manifest.static_requirements.runtimes:
        if not _runtime_satisfies(requirement, runtimes):
            reasons.append(ErrorCode("RUNTIME_NOT_FOUND"))
    for requirement in manifest.static_requirements.software:
        matching = tuple(item for item in software if item.name == requirement.name)
        if not matching:
            reasons.append(ErrorCode("SOFTWARE_NOT_FOUND"))
        else:
            if requirement.version is not None and not any(
                _version_satisfies(item.version, requirement.version) for item in matching
            ):
                reasons.append(ErrorCode("SOFTWARE_VERSION_MISMATCH"))
            if requirement.license_required and not any(
                item.properties.get("license_available") is True for item in matching
            ):
                reasons.append(ErrorCode("SOFTWARE_NOT_FOUND"))
    for requirement in manifest.static_requirements.resources:
        if not _resource_quantity_satisfies(requirement, resources):
            reasons.append(ErrorCode("RESOURCE_UNAVAILABLE"))

    unique_reasons = _unique_codes(tuple(reasons))
    if unique_reasons:
        return PluginAvailabilityEvaluation(PluginAvailability.BLOCKED, unique_reasons)
    return PluginAvailabilityEvaluation(PluginAvailability.AVAILABLE)


class AgentCapabilitySnapshotService:
    """生成 Agent 的版本化  能力快照。"""

    def __init__(
        self,
        node_id: BusinessId,
        session_id: SessionId,
        registry: PluginRegistry,
        *,
        tags: tuple[str, ...] = (),
        maintenance_state: AgentMaintenanceState = AgentMaintenanceState.IDLE,
        capability_scanner: Callable[[], NodeCapabilities] | None = None,
        runtime_discoverer: Callable[[NodeCapabilities], tuple[RuntimeCapability, ...]] | None = None,
        software_discoverer: Callable[[NodeCapabilities], tuple[SoftwareCapability, ...]] | None = None,
        resource_discoverer: Callable[[NodeCapabilities], tuple[ResourceCapability, ...]] | None = None,
        resource_providers: ResourceProviderRegistry | None = None,
        runtime_providers: tuple[RuntimeProvider, ...] = (),
        software_providers: tuple[SoftwareProvider, ...] = (),
        health_checker: Callable[[PluginManifest], tuple[ErrorCode, ...]] | None = None,
        revision_cache: CapabilityRevisionCache | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._node_id = node_id
        self._session_id = session_id
        self._registry = registry
        self._tags = tags
        self._maintenance_state = maintenance_state
        self._capability_scanner = capability_scanner or scan_base_capabilities
        self._runtime_discoverer = runtime_discoverer or self._runtime_capabilities
        self._software_discoverer = software_discoverer or (lambda _capabilities: discover_software())
        self._resource_discoverer = resource_discoverer or self._resource_capabilities
        self._resource_providers = resource_providers
        self._runtime_providers = runtime_providers
        self._software_providers = software_providers
        self._health_checker = health_checker
        self._revision_cache = revision_cache or CapabilityRevisionCache()
        self._now = now or (lambda: datetime.now(UTC))

    def build_snapshot(self) -> NodeCapabilitySnapshot:
        """扫描本机并构造下一版本快照。"""
        try:
            capabilities = self._capability_scanner()
        except Exception:
            logger.exception("Agent 能力扫描失败，使用空能力快照")
            capabilities = NodeCapabilities()

        runtimes = self._runtimes_with_plugins(capabilities)
        software = self._software_with_plugins(capabilities)
        discovered_resources = self._resource_discoverer(capabilities)
        if self._resource_providers is not None:
            discovered_resources = self._resource_providers.discover()
            provider_types = self._resource_providers.resource_types
            resources = discovered_resources + tuple(
                resource
                for resource in self._resource_capabilities(capabilities)
                if resource.resource_type not in provider_types
            )
        else:
            resources = discovered_resources
        inventory: list[PluginInventoryItem] = []
        executors: list[ExecutorCapability] = []
        checked_at = self._now()

        for installed in self._registry.list():
            try:
                manifest = PluginManifest.model_validate_json(
                    installed.manifest_path.read_text(encoding="utf-8")
                )
                health_errors = self._health_errors(manifest)
                evaluation = evaluate_plugin_availability(
                    manifest,
                    runtimes=runtimes,
                    software=software,
                    resources=resources,
                    health_errors=health_errors,
                )
            except Exception:
                logger.exception("插件库存检查失败: %s", installed.install_path)
                manifest = None
                evaluation = PluginAvailabilityEvaluation(
                    PluginAvailability.ERROR,
                    (ErrorCode("PLUGIN_MANIFEST_INVALID"),),
                )

            if manifest is not None:
                inventory.append(
                    PluginInventoryItem(
                        plugin_id=manifest.id,
                        point=manifest.point,
                        version=manifest.version,
                        archive_sha256=installed.ref.archive_sha256,
                        availability=evaluation.availability,
                        unavailable_reasons=evaluation.unavailable_reasons,
                        checked_at=checked_at,
                    )
                )
                if (
                    manifest.point is PluginPoint.EXECUTOR
                    and evaluation.availability is PluginAvailability.AVAILABLE
                ):
                    executors.append(
                        ExecutorCapability(
                            plugin_id=manifest.id,
                            version=manifest.version,
                            capabilities=manifest.capabilities,
                        )
                    )

        return NodeCapabilitySnapshot(
            schema_version=2,
            node_id=self._node_id,
            session_id=self._session_id,
            revision=self._revision_cache.next(self._node_id, self._session_id),
            reported_at=checked_at,
            tags=self._tags,
            executors=tuple(executors),
            runtimes=runtimes,
            software=software,
            resources=resources,
            system=capabilities.system,
            maintenance_state=self._maintenance_state,
            plugin_inventory=tuple(inventory),
        )

    def _runtimes_with_plugins(self, capabilities: NodeCapabilities) -> tuple[RuntimeCapability, ...]:
        """合并 runtime 插件 Provider 与本机基础扫描结果。

        未安装 runtime 插件时完全走 ``runtime_discoverer``（默认本机语言扫描）。
        安装了插件时，Provider 拥有其声明的 ``runtime_type``（基础扫描同类别不再
        重复上报），基础扫描只补充 Provider 未覆盖的类别。单个 Provider 发现失败
        只跳过该 Provider 并记录日志，不影响其他 Provider 与基础扫描。
        """
        if not self._runtime_providers:
            return self._runtime_discoverer(capabilities)
        provider_types: set[str] = set()
        discovered: list[RuntimeCapability] = []
        for provider in self._runtime_providers:
            provider_types.add(provider.runtime_type)
            try:
                discovered.extend(_discover_runtime_provider(provider))
            except RuntimeDiscoveryError:
                logger.exception("runtime Provider 契约违规，已跳过: provider=%s", provider.provider_id)
        base = self._runtime_capabilities(capabilities)
        return tuple(discovered) + tuple(
            item for item in base if item.runtime_type not in provider_types
        )

    def _software_with_plugins(self, capabilities: NodeCapabilities) -> tuple[SoftwareCapability, ...]:
        """合并 software 插件 Provider 与本机基础探测结果。

        未安装 software 插件时完全走 ``software_discoverer``（默认探测 CANoe/
        Vector Driver）。安装了插件时，Provider 拥有其声明的 ``name``（基础探测
        同名不再重复上报），基础探测只补充 Provider 未覆盖的软件。
        """
        if not self._software_providers:
            return self._software_discoverer(capabilities)
        provider_names: set[str] = set()
        discovered: list[SoftwareCapability] = []
        for provider in self._software_providers:
            provider_names.add(provider.name)
            try:
                discovered.extend(_discover_software_provider(provider))
            except SoftwareDiscoveryError:
                logger.exception("software Provider 契约违规，已跳过: provider=%s", provider.provider_id)
        base = discover_software()
        return tuple(discovered) + tuple(
            item for item in base if item.name not in provider_names
        )

    def _health_errors(self, manifest: PluginManifest) -> tuple[ErrorCode, ...]:
        if self._health_checker is None:
            return ()
        try:
            return self._health_checker(manifest)
        except Exception:
            logger.exception("插件健康检查失败: %s@%s", manifest.id.root, manifest.version.root)
            return (ErrorCode("PLUGIN_SYNC_FAILED"),)

    def _runtime_capabilities(self, capabilities: NodeCapabilities) -> tuple[RuntimeCapability, ...]:
        if capabilities.language is None:
            return ()
        return tuple(
            RuntimeCapability(
                provider_id="agent.discovery",
                runtime_id=f"{runtime.name}:{runtime.version.root}",
                runtime_type=runtime.name,
                version=runtime.version,
            )
            for runtime in capabilities.language.runtimes
        )

    def _resource_capabilities(self, capabilities: NodeCapabilities) -> tuple[ResourceCapability, ...]:
        resources: list[ResourceCapability] = []
        if capabilities.vehicle is not None:
            for vendor in capabilities.vehicle.vendors:
                for bus in vendor.buses:
                    for channel in bus.channels:
                        resources.append(
                            ResourceCapability(
                                resource_id=stable_id(
                                    f"{self._node_id.root}:vehicle:{vendor.name}:{bus.bus_type}:{channel.name}"
                                ),
                                provider_id=f"agent.{vendor.name}",
                                resource_type=bus.bus_type,
                                vendor=vendor.name,
                                model=channel.hardware_model,
                                channel=channel.name,
                                labels={"source": "vehicle"},
                                health=(
                                    ResourceHealth.READY
                                    if channel.enabled
                                    else ResourceHealth.UNAVAILABLE
                                ),
                            )
                        )
        if capabilities.serial is not None:
            for port in capabilities.serial.ports:
                resources.append(
                    ResourceCapability(
                        resource_id=stable_id(f"{self._node_id.root}:serial:{port.function}:{port.port}"),
                        provider_id="agent.serial",
                        resource_type="serial",
                        channel=port.port,
                        function=port.function,
                        labels={"source": "serial"},
                        health=ResourceHealth.READY if port.enabled else ResourceHealth.UNAVAILABLE,
                    )
                )
        return tuple(resources)


def _discover_runtime_provider(provider: RuntimeProvider) -> tuple[RuntimeCapability, ...]:
    """调用单个 runtime Provider 的 discover 并校验身份一致性。

    Provider 契约要求返回的每个 ``RuntimeCapability`` 与 Provider 声明的
    ``runtime_type``/``provider_id`` 一致；不一致视为契约违规抛出
    ``RuntimeDiscoveryError``，由调用方按“单个 Provider 失败”处理。
    """
    try:
        discovered = tuple(provider.discover())
    except Exception:
        logger.exception("runtime Provider 发现失败: provider=%s", provider.provider_id)
        return ()
    for item in discovered:
        if item.runtime_type != provider.runtime_type:
            raise RuntimeDiscoveryError(
                f"runtime Provider 类型不一致: expected={provider.runtime_type} actual={item.runtime_type}"
            )
        if item.provider_id != provider.provider_id:
            raise RuntimeDiscoveryError(
                f"runtime Provider 身份不一致: expected={provider.provider_id} actual={item.provider_id}"
            )
    return discovered


def _discover_software_provider(provider: SoftwareProvider) -> tuple[SoftwareCapability, ...]:
    """调用单个 software Provider 的 discover 并校验身份一致性。"""
    try:
        discovered = tuple(provider.discover())
    except Exception:
        logger.exception("software Provider 发现失败: provider=%s", provider.provider_id)
        return ()
    for item in discovered:
        if item.name != provider.name:
            raise SoftwareDiscoveryError(
                f"software Provider 名称不一致: expected={provider.name} actual={item.name}"
            )
        if item.provider_id != provider.provider_id:
            raise SoftwareDiscoveryError(
                f"software Provider 身份不一致: expected={provider.provider_id} actual={item.provider_id}"
            )
    return discovered


def _runtime_satisfies(
    requirement: RuntimeRequirement,
    runtimes: tuple[RuntimeCapability, ...],
) -> bool:
    candidates = tuple(runtime for runtime in runtimes if runtime.runtime_type == requirement.runtime_type)
    return requirement.version is None or any(
        _version_satisfies(runtime.version, requirement.version) for runtime in candidates
    )


def _version_satisfies(actual: CapabilityVersion, requirement: VersionConstraint) -> bool:
    actual_parts = _version_parts(actual)
    if requirement.exact is not None and actual_parts != _version_parts(requirement.exact):
        return False
    if requirement.minimum is not None and actual_parts < _version_parts(requirement.minimum):
        return False
    return requirement.maximum is None or actual_parts <= _version_parts(requirement.maximum)


def _resource_quantity_satisfies(
    requirement: ResourceRequirement,
    resources: tuple[ResourceCapability, ...],
) -> bool:
    matches = tuple(resource for resource in resources if _resource_matches(resource, requirement))
    return len(matches) >= requirement.quantity


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


def _version_parts(version: CapabilityVersion | Version) -> tuple[int, ...]:
    return tuple(int(part) for part in version.root.removeprefix("v").split("."))


def _unique_codes(codes: tuple[ErrorCode, ...]) -> tuple[ErrorCode, ...]:
    result: list[ErrorCode] = []
    for code in codes:
        if code not in result:
            result.append(code)
    return tuple(result)


__all__ = [
    "AgentCapabilitySnapshotService",
    "CapabilityRevisionCache",
    "PluginAvailabilityEvaluation",
    "evaluate_plugin_availability",
]
