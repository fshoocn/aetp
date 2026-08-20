"""强类型硬件能力匹配（P4.5 规格模式重构，§18.5）。

能力匹配采用规格模式（Specification）：每个能力类别是一个规格
（``CapabilitySpec``），求值返回失败原因，空序列表示满足；``AllOf``
负责组合，任一类别失败即整体失败。

- ``VehicleSpec``：厂商 / 总线 / 通道数量和通道名
- ``LanguageSpec``：语言运行时和语义化版本
- ``SystemSpec``：操作系统、版本、内存、CPU
- ``SerialSpec``：功能到串口的映射和启用状态
- ``TagSpec``：节点标签

新增能力类别只需新增一个规格类并加入默认组合，求值器与其它类别不受影响。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from aetp_protocol.capabilities import (
    BusRequirement,
    HardwareRequirements,
    NodeCapabilities,
    NumericConstraint,
    Version,
    VersionConstraint,
)


class CapabilityRequirementError(ValueError):
    """硬件需求不合法或无法由对应 matcher 求值。"""


@dataclass(frozen=True)
class CapabilityMatch:
    """节点能力匹配结果。"""

    matched: bool
    failures: tuple[str, ...] = ()


class CapabilitySpec(Protocol):
    """能力规格：求值返回失败原因，空序列表示满足。"""

    def evaluate(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """返回该规格的失败原因；空表示满足。"""
        ...


class AllOf:
    """AND 组合规格：任一子规格失败即整体失败。"""

    def __init__(self, specs: Iterable[CapabilitySpec]) -> None:
        self._specs = tuple(specs)

    def evaluate(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> tuple[str, ...]:
        failures: list[str] = []
        for spec in self._specs:
            failures.extend(spec.evaluate(capabilities, requirements, tags))
        return tuple(failures)


class VehicleSpec:
    """车载能力规格：厂商 -> 总线 -> 已启用通道。"""

    def evaluate(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> tuple[str, ...]:
        requirement = requirements.vehicle
        if requirement is None:
            return ()
        if capabilities.vehicle is None:
            return ("节点未上报 vehicle 能力",)

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
        return tuple(failures)

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


class LanguageSpec:
    """语言运行时规格：按语言名查找，按 VersionConstraint 比较版本。"""

    def evaluate(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> tuple[str, ...]:
        if not requirements.languages:
            return ()
        if capabilities.language is None:
            return ("节点未上报 language 能力",)

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
        return tuple(failures)


class SystemSpec:
    """系统规格：操作系统/版本、内存、CPU 各自使用专用约束。"""

    def evaluate(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> tuple[str, ...]:
        requirement = requirements.system
        if requirement is None:
            return ()
        if capabilities.system is None:
            return ("节点未上报 system 能力",)

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
        return tuple(failures)


class SerialSpec:
    """串口功能规格：按功能名定位端口，可选约束端口号和 enabled。"""

    def evaluate(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> tuple[str, ...]:
        if not requirements.serial_ports:
            return ()
        if capabilities.serial is None:
            return ("节点未上报 serial 能力",)

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
        return tuple(failures)


class TagSpec:
    """节点标签规格。"""

    def evaluate(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> tuple[str, ...]:
        actual = set(tags)
        return tuple(
            f"缺少标签: {tag}"
            for tag in requirements.required_tags
            if tag not in actual
        )


def default_capability_spec() -> CapabilitySpec:
    """默认能力规格组合：车载 + 语言 + 系统 + 串口 + 标签。"""
    return AllOf(
        (
            VehicleSpec(),
            LanguageSpec(),
            SystemSpec(),
            SerialSpec(),
            TagSpec(),
        )
    )


class CapabilityEvaluator:
    """唯一能力求值器：组合全部类别规格，返回 CapabilityMatch。"""

    def __init__(self, spec: CapabilitySpec | None = None) -> None:
        self._spec = spec or default_capability_spec()

    def evaluate(
        self,
        capabilities: NodeCapabilities,
        requirements: HardwareRequirements,
        tags: Sequence[str] = (),
    ) -> CapabilityMatch:
        failures = self._spec.evaluate(capabilities, requirements, tags)
        return CapabilityMatch(matched=not failures, failures=failures)


_DEFAULT_EVALUATOR = CapabilityEvaluator()


def evaluate_capability(
    requirements: HardwareRequirements,
    capabilities: NodeCapabilities,
    tags: Sequence[str] = (),
) -> CapabilityMatch:
    """类型安全的公共匹配入口。"""
    return _DEFAULT_EVALUATOR.evaluate(capabilities, requirements, tags)


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
    return not (requirement.maximum is not None and actual_parts > _version_parts(requirement.maximum))


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
