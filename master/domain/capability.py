"""强类型硬件能力匹配器（P4.5 重构，§18.5）。

匹配器只接收公共协议模型：``NodeCapabilities`` 与 ``HardwareRequirements``。
不同能力类型由不同 matcher 负责：

- ``VehicleMatcher``：厂商 / 总线 / 通道数量和通道名
- ``LanguageMatcher``：语言运行时和语义化版本
- ``SystemMatcher``：操作系统、版本、内存、CPU
- ``SerialMatcher``：功能到串口的映射和启用状态

新增厂商、总线、语言或串口功能只增加模型数据，不修改通用匹配逻辑；
新增能力类别时新增一个公共模型和一个 matcher，避免通用字符串路径逐渐变成隐式协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from aetp_protocol.capabilities import (
    BusRequirement,
    DeviceRequirement,
    HardwareRequirements,
    LanguageRequirement,
    NumericConstraint,
    NodeCapabilities,
    PhysicalDeviceCapability,
    SerialPortRequirement,
    SystemRequirement,
    VehicleRequirement,
    Version,
    VersionConstraint,
)


class CapabilityRequirementError(ValueError):
    """硬件需求不合法或无法由对应 matcher 求值。"""


class PhysicalDeviceMatcher:
    """物理资源能力判定器。

    设备 ID 的精确约束由调度器处理；本类只判断设备自身能力属性，
    例如 ``vector/1640/can1`` 或 ``relay_board``。
    """

    def matches(
        self,
        capability: PhysicalDeviceCapability,
        requirement: DeviceRequirement,
    ) -> bool:
        return (
            capability.resource_type == requirement.resource_type
            and _same_if_set(capability.vendor, requirement.vendor)
            and _same_if_set(capability.model, requirement.model)
            and _same_if_set(capability.channel, requirement.channel)
            and _same_if_set(capability.function, requirement.function)
        )


def _same_if_set(actual: str | None, expected: str | None) -> bool:
    return expected is None or actual == expected


@dataclass(frozen=True)
class CapabilityMatch:
    """节点能力匹配结果。"""

    matched: bool
    failures: tuple[str, ...] = ()


class CapabilityMatcher(Protocol):
    """能力类别 matcher 端口。"""

    def match(self, capabilities: NodeCapabilities, requirements: HardwareRequirements) -> list[str]:
        """返回该类别的失败原因；空列表表示该类别满足。"""
        ...


class VehicleMatcher:
    """车载能力判定：厂商 -> 总线 -> 已启用通道。"""

    def match(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
    ) -> list[str]:
        requirement = requirements.vehicle
        if requirement is None:
            return []
        if capabilities.vehicle is None:
            return ["节点未上报 vehicle 能力"]

        failures: list[str] = []
        for bus_requirement in requirement.all_of:
            if not self._match_bus(capabilities, bus_requirement):
                failures.append(self._failure(bus_requirement))
        if requirement.any_of and not any(
            self._match_bus(capabilities, bus_requirement)
            for bus_requirement in requirement.any_of
        ):
            alternatives = "；".join(
                self._failure(bus_requirement) for bus_requirement in requirement.any_of
            )
            failures.append(f"vehicle 任一总线需求均不满足: {alternatives}")
        return failures

    @staticmethod
    def _match_bus(capabilities: NodeCapabilities, requirement: BusRequirement) -> bool:
        assert capabilities.vehicle is not None
        vendors = capabilities.vehicle.vendors
        if requirement.vendor is not None:
            vendors = tuple(v for v in vendors if v.name == requirement.vendor)
        for vendor in vendors:
            for bus in vendor.buses:
                if bus.bus_type != requirement.bus_type:
                    continue
                enabled_channels = tuple(
                    channel
                    for channel in bus.channels
                    if channel.enabled
                    and (
                        requirement.hardware_model is None
                        or channel.hardware_model == requirement.hardware_model
                    )
                )
                names = {channel.name for channel in enabled_channels}
                if (
                    requirement.minimum_channels is not None
                    and len(enabled_channels) < requirement.minimum_channels
                ):
                    continue
                if not set(requirement.required_channels).issubset(names):
                    continue
                return True
        return False

    @staticmethod
    def _failure(requirement: BusRequirement) -> str:
        vendor = f" vendor={requirement.vendor}" if requirement.vendor else ""
        details: list[str] = []
        if requirement.minimum_channels is not None:
            details.append(f"至少 {requirement.minimum_channels} 个通道")
        if requirement.required_channels:
            details.append(f"需要通道 {', '.join(requirement.required_channels)}")
        if requirement.hardware_model:
            details.append(f"硬件型号 {requirement.hardware_model}")
        return f"vehicle 总线不满足: {vendor} bus={requirement.bus_type}（{'，'.join(details)}）"


class LanguageMatcher:
    """语言运行时判定：按语言名查找，按 VersionConstraint 比较版本。"""

    def match(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
    ) -> list[str]:
        if not requirements.languages:
            return []
        if capabilities.language is None:
            return ["节点未上报 language 能力"]

        failures: list[str] = []
        for requirement in requirements.languages:
            runtimes = tuple(
                runtime
                for runtime in capabilities.language.runtimes
                if runtime.name == requirement.name
            )
            if not any(
                requirement.version is None
                or _matches_version(runtime.version, requirement.version)
                for runtime in runtimes
            ):
                failures.append(f"language 不满足: {requirement.name}")
        return failures


class SystemMatcher:
    """系统判定：操作系统/版本、内存、CPU 各自使用专用约束。"""

    def match(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
    ) -> list[str]:
        requirement = requirements.system
        if requirement is None:
            return []
        if capabilities.system is None:
            return ["节点未上报 system 能力"]

        failures: list[str] = []
        if requirement.operating_system is not None:
            os_requirement = requirement.operating_system
            operating_system = capabilities.system.operating_system
            if operating_system is None:
                failures.append("system 未上报 operating_system")
            else:
                if (
                    os_requirement.name is not None
                    and operating_system.name != os_requirement.name
                ):
                    failures.append(
                        f"操作系统名称不匹配: 期望 {os_requirement.name}，"
                        f"实际 {operating_system.name}"
                    )
                if (
                    os_requirement.version is not None
                    and not _matches_version(operating_system.version, os_requirement.version)
                ):
                    failures.append("操作系统版本不满足")

        failures.extend(
            _numeric_failure(
                "system.memory_mb",
                capabilities.system.memory_mb,
                requirement.memory_mb,
            )
        )
        failures.extend(
            _numeric_failure(
                "system.cpu_cores",
                capabilities.system.cpu_cores,
                requirement.cpu_cores,
            )
        )
        return failures


class SerialMatcher:
    """串口功能判定：按功能名定位端口，可选约束端口号和 enabled。"""

    def match(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
    ) -> list[str]:
        if not requirements.serial_ports:
            return []
        if capabilities.serial is None:
            return ["节点未上报 serial 能力"]

        failures: list[str] = []
        for requirement in requirements.serial_ports:
            candidates = tuple(
                port
                for port in capabilities.serial.ports
                if port.function == requirement.function
                and (requirement.port is None or port.port == requirement.port)
                and port.enabled == requirement.enabled
            )
            if not candidates:
                suffix = (
                    f" port={requirement.port}" if requirement.port is not None else ""
                )
                failures.append(
                    f"serial 功能不满足: function={requirement.function}{suffix}"
                )
        return failures


class HardwareCapabilityMatcher:
    """聚合各能力类别 matcher；类别之间是 AND 关系。"""

    def __init__(
        self,
        *,
        vehicle: VehicleMatcher | None = None,
        language: LanguageMatcher | None = None,
        system: SystemMatcher | None = None,
        serial: SerialMatcher | None = None,
    ) -> None:
        self._vehicle = vehicle or VehicleMatcher()
        self._language = language or LanguageMatcher()
        self._system = system or SystemMatcher()
        self._serial = serial or SerialMatcher()

    def match(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> CapabilityMatch:
        failures: list[str] = []
        failures.extend(self._vehicle.match(capabilities, requirements))
        failures.extend(self._language.match(capabilities, requirements))
        failures.extend(self._system.match(capabilities, requirements))
        failures.extend(self._serial.match(capabilities, requirements))
        required_tags = set(requirements.required_tags)
        actual_tags = set(tags)
        failures.extend(
            f"缺少标签: {tag}"
            for tag in requirements.required_tags
            if tag not in actual_tags
        )
        return CapabilityMatch(matched=not failures, failures=tuple(failures))


_DEFAULT_MATCHER = HardwareCapabilityMatcher()


def match_capability(
    requirements: HardwareRequirements,
    capabilities: NodeCapabilities,
    tags: Sequence[str] = (),
) -> CapabilityMatch:
    """类型安全的公共匹配入口（具体逻辑由类别 matcher 负责）。"""
    return _DEFAULT_MATCHER.match(capabilities, requirements, tags)


def list_capability_paths(capabilities: NodeCapabilities) -> list[str]:
    """列出强类型能力对象的叶子路径，供错误诊断使用。"""
    paths: list[str] = []
    if capabilities.vehicle is not None:
        for vendor in capabilities.vehicle.vendors:
            for bus in vendor.buses:
                for channel in bus.channels:
                    paths.append(
                        f"vehicle.vendor.{vendor.name}.bus.{bus.bus_type}.channel.{channel.name}"
                    )
    if capabilities.language is not None:
        for runtime in capabilities.language.runtimes:
            paths.append(f"language.{runtime.name}.version")
    if capabilities.system is not None:
        if capabilities.system.operating_system is not None:
            paths.append("system.operating_system.name")
            paths.append("system.operating_system.version")
        if capabilities.system.memory_mb is not None:
            paths.append("system.memory_mb")
        if capabilities.system.cpu_cores is not None:
            paths.append("system.cpu_cores")
    if capabilities.serial is not None:
        for port in capabilities.serial.ports:
            paths.extend(
                (
                    f"serial.{port.function}.port",
                    f"serial.{port.function}.enabled",
                )
            )
    return sorted(set(paths))


def _matches_version(actual: Version, requirement: VersionConstraint) -> bool:
    actual_parts = _version_parts(actual)
    if requirement.exact is not None and actual_parts != _version_parts(requirement.exact):
        return False
    if requirement.minimum is not None and actual_parts < _version_parts(requirement.minimum):
        return False
    if requirement.maximum is not None and actual_parts > _version_parts(requirement.maximum):
        return False
    return True


def _version_parts(version: Version) -> tuple[int, ...]:
    value = version.root.removeprefix("v")
    return tuple(int(part) for part in value.split("."))


def _numeric_failure(
    name: str,
    actual: int | None,
    requirement: NumericConstraint | None,
) -> list[str]:
    if requirement is None:
        return []
    if actual is None:
        return [f"{name} 未上报"]
    if requirement.exact is not None and actual != requirement.exact:
        return [f"{name} 期望等于 {requirement.exact}，实际 {actual}"]
    if requirement.minimum is not None and actual < requirement.minimum:
        return [f"{name} 期望至少 {requirement.minimum}，实际 {actual}"]
    if requirement.maximum is not None and actual > requirement.maximum:
        return [f"{name} 期望至多 {requirement.maximum}，实际 {actual}"]
    return []
