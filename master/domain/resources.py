"""物理资源匹配与分配（P4.6 重构，§18.5/§18.6）。

资源与能力分离：能力回答“节点有没有这种硬件”，本模块回答“现在能分到
哪些具体设备”。回溯分配只出现在这里，调度编排不再包含分配算法。

- ``PhysicalDeviceMatcher``：资源静态属性判定（不含标签）
- ``SwitchRoute``：经切换开关的分配路径
- ``Allocation``：一次分配的设备 + 设备→切换路径映射
- ``NodeSchedulingState``：节点资源预留快照
- ``ResourceAssignment``：一次 Attempt 的完整资源集合（含切换路径）
- ``ResourceAllocator``：原子回溯分配 + 预留 + 切换开关展开
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import combinations

from aetp_protocol.capabilities import DeviceRequirement, PhysicalDeviceCapability

from master.domain.enums import DeviceStatus
from master.domain.models import Device, Node


@dataclass(frozen=True)
class SwitchRoute:
    """经切换开关的分配路径。"""

    switch_device_id: str
    port: str


@dataclass(frozen=True)
class Allocation:
    """一次分配的完整结果：设备 + 设备→切换路径映射。"""

    devices: tuple[Device, ...] = ()
    routes_by_device: Mapping[str, SwitchRoute] = field(default_factory=dict)


class PhysicalDeviceMatcher:
    """物理资源静态属性判定：resource_type + 厂商/型号/通道/功能。

    标签与切换开关展开由分配器处理，不在此处。
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


def _has_labels(actual: dict[str, str], required: dict[str, str]) -> bool:
    """硬约束：required 全部命中才算满足。"""
    return all(actual.get(key) == value for key, value in required.items())


def _preferred_label_score(
    actual: dict[str, str], preferred: dict[str, str]
) -> int:
    """软约束得分：preferred 命中越多越优先。"""
    return sum(1 for key, value in preferred.items() if actual.get(key) == value)


@dataclass(frozen=True)
class NodeSchedulingState:
    """调度用节点资源预留快照。"""

    node: Node
    reserved_device_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ResourceAssignment:
    """一次 Attempt 在某个节点上获得的完整资源集合。"""

    node: Node
    devices: tuple[Device, ...] = ()
    routes_by_device: Mapping[str, SwitchRoute] = field(default_factory=dict)

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(device.device_id for device in self.devices)


class ResourceAllocator:
    """原子分配一个 Shard 所需的全部空闲物理资源。"""

    def __init__(self, matcher: PhysicalDeviceMatcher | None = None) -> None:
        self._matcher = matcher or PhysicalDeviceMatcher()

    def supports(
        self, node: Node, requirements: tuple[DeviceRequirement, ...]
    ) -> bool:
        """节点是否具备资源能力（忽略占用状态）。"""
        return (
            self.allocate(
                node, requirements, frozenset(), include_occupied=True
            )
            is not None
        )

    def allocate(
        self,
        node: Node,
        requirements: tuple[DeviceRequirement, ...],
        reserved_device_ids: frozenset[str],
        *,
        include_occupied: bool = False,
    ) -> Allocation | None:
        """回溯分配；任一资源不可用返回 None（整组排队）。"""
        if not requirements:
            return Allocation()
        available = tuple(
            device
            for device in node.devices
            if device.device_id not in reserved_device_ids
            and (
                include_occupied
                or (device.online and device.status is not DeviceStatus.BUSY)
            )
        )
        ordered = tuple(
            sorted(requirements, key=_requirement_specificity, reverse=True)
        )
        return self._allocate_groups(
            available,
            ordered,
            index=0,
            used_devices=frozenset(),
            used_ports=frozenset(),
        )

    def reserve(
        self, state: NodeSchedulingState, device_ids: Iterable[str]
    ) -> NodeSchedulingState:
        """预留本轮已分配的资源。"""
        available = set(
            _available_device_ids(state.node, state.reserved_device_ids)
        )
        requested = frozenset(device_ids)
        if not requested.issubset(available):
            raise ValueError(
                f"设备不可预留: node={state.node.node_id} devices={sorted(requested)}"
            )
        return NodeSchedulingState(
            node=state.node,
            reserved_device_ids=state.reserved_device_ids | requested,
        )

    def _allocate_groups(
        self,
        available: tuple[Device, ...],
        requirements: tuple[DeviceRequirement, ...],
        *,
        index: int,
        used_devices: frozenset[str],
        used_ports: frozenset[str],
    ) -> Allocation | None:
        if index == len(requirements):
            return Allocation()
        requirement = requirements[index]
        matched: list[tuple[Device, SwitchRoute | None]] = []
        for device in available:
            if device.device_id in used_devices:
                continue
            match = self._match_device(device, requirement, used_ports)
            if match is not None:
                matched.append(match)
        matches = tuple(
            sorted(
                matched,
                key=lambda match: _preferred_label_score(
                    match[0].capability.labels, requirement.preferred_labels
                ),
                reverse=True,
            )
        )
        for selected in combinations(matches, requirement.quantity):
            selected_devices = tuple(match[0] for match in selected)
            selected_routes = tuple(
                match[1] for match in selected if match[1] is not None
            )
            selected_device_ids = frozenset(
                device.device_id for device in selected_devices
            )
            if requirement.device_ids and not set(requirement.device_ids).issubset(
                selected_device_ids
            ):
                continue
            selected_ports = frozenset(
                f"{route.switch_device_id}:{route.port}"
                for route in selected_routes
            )
            if selected_ports.intersection(used_ports):
                continue
            remainder = self._allocate_groups(
                available,
                requirements,
                index=index + 1,
                used_devices=used_devices | selected_device_ids,
                used_ports=used_ports | selected_ports,
            )
            if remainder is not None:
                routes = {
                    device.device_id: route
                    for device, route in selected
                    if route is not None
                }
                return Allocation(
                    devices=selected_devices + remainder.devices,
                    routes_by_device={**routes, **remainder.routes_by_device},
                )
        return None

    def _match_device(
        self,
        device: Device,
        requirement: DeviceRequirement,
        used_ports: frozenset[str],
    ) -> tuple[Device, SwitchRoute | None] | None:
        """匹配单个设备：自身标签或经切换开关的端口标签。"""
        if not self._matcher.matches(device.capability, requirement):
            return None
        if _has_labels(device.capability.labels, requirement.required_labels):
            return (device, None)
        if (
            requirement.allow_switching
            and device.capability.connection is not None
        ):
            connection = device.capability.connection
            for port in connection.ports:
                if _has_labels(port.labels, requirement.required_labels):
                    key = f"{connection.switch_device_id}:{port.port}"
                    if key not in used_ports:
                        return (
                            device,
                            SwitchRoute(
                                switch_device_id=connection.switch_device_id,
                                port=port.port,
                            ),
                        )
        return None


def _available_device_ids(
    node: Node, reserved_device_ids: frozenset[str]
) -> tuple[str, ...]:
    """返回在线、非 BUSY 且未被本轮调度预留的设备。"""
    return tuple(
        device.device_id
        for device in node.devices
        if device.online
        and device.status is not DeviceStatus.BUSY
        and device.device_id not in reserved_device_ids
    )


def _requirement_specificity(requirement: DeviceRequirement) -> tuple[int, int]:
    """让指定 ID/属性的需求先分配，避免被宽泛需求抢占。"""
    constrained_fields = sum(
        value is not None
        for value in (
            requirement.vendor,
            requirement.model,
            requirement.channel,
            requirement.function,
        )
    )
    return (
        len(requirement.device_ids) * 100 + constrained_fields,
        requirement.quantity,
    )
