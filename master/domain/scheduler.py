"""Shard 候选节点选择策略（P4.6，§18.5/§18.6）。

本模块只负责调度决策，不读取数据库、不创建 Attempt、不发布 MQTT。
应用层提供节点和活动执行快照后，本模块执行：

* 在线、启用和硬件能力筛选；
* 脚本物理资源需求的完整匹配与分配；
* failover 已尝试节点排除；
* 负载最低优先、最近心跳优先、业务 ID 兜底的确定性排序。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from typing import Iterable

from aetp_protocol.capabilities import DeviceRequirement, HardwareRequirements

from master.domain.capability import HardwareCapabilityMatcher, PhysicalDeviceMatcher
from master.domain.enums import DeviceStatus, NodeStatus, ShardAttemptStatus
from master.domain.models import Device, Node, RunShard, ShardAttempt


@dataclass(frozen=True)
class NodeSchedulingState:
    """调度器使用的单节点设备状态快照。"""

    node: Node
    reserved_device_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ResourceAssignment:
    """一个 Shard 在同一 Node 上获得的完整物理资源集合。"""

    node: Node
    devices: tuple[Device, ...] = ()

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(device.device_id for device in self.devices)


class ShardScheduler:
    """纯函数式候选节点选择器。"""

    def __init__(self, matcher: HardwareCapabilityMatcher | None = None) -> None:
        self._matcher = matcher or HardwareCapabilityMatcher()
        self._device_matcher = PhysicalDeviceMatcher()

    def select_node(
        self,
        *,
        shard: RunShard,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        excluded_node_ids: Iterable[str] = (),
    ) -> Node | None:
        """选择一个能力满足且设备可用的节点；没有可用节点时返回 ``None``。"""

        assignment = self.select_assignment(
            shard=shard,
            requirements=requirements,
            candidates=candidates,
            excluded_node_ids=excluded_node_ids,
        )
        return assignment.node if assignment is not None else None

    def select_assignment(
        self,
        *,
        shard: RunShard,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        excluded_node_ids: Iterable[str] = (),
    ) -> ResourceAssignment | None:
        """选择节点及该 Shard 所需的全部空闲设备。"""

        eligible = self.eligible_candidates(
            shard=shard,
            requirements=requirements,
            candidates=candidates,
            excluded_node_ids=excluded_node_ids,
        )
        if not eligible:
            return None
        state = eligible[0]
        devices = self._allocate_devices(
            state.node,
            requirements.devices,
            state.reserved_device_ids,
        )
        if devices is None:
            return None
        return ResourceAssignment(node=state.node, devices=devices)

    def eligible_candidates(
        self,
        *,
        shard: RunShard,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        excluded_node_ids: Iterable[str] = (),
    ) -> tuple[NodeSchedulingState, ...]:
        """返回能力满足且设备未占用的节点。"""

        excluded = frozenset(excluded_node_ids)
        eligible: list[NodeSchedulingState] = []
        for state in candidates:
            node = state.node
            if node.node_id in excluded:
                continue
            if not node.enabled or not node.online:
                continue
            if node.status in (NodeStatus.OFFLINE, NodeStatus.DISABLED):
                continue
            if not self._matcher.match(
                node.capabilities, requirements, node.tags
            ).matched:
                continue
            if self._allocate_devices(
                node, requirements.devices, state.reserved_device_ids
            ) is None:
                continue
            eligible.append(state)

        eligible.sort(key=self._priority_key)
        return tuple(eligible)

    def select_failover_node(
        self,
        *,
        shard: RunShard,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        attempts: Iterable[ShardAttempt],
    ) -> Node | None:
        """为同一 Shard 选择 failover 节点。

        D-20 要求换节点时保留历史 Attempt，因此所有历史尝试过的节点都
        排除；若没有其它节点可用，返回 ``None``，由应用层保持等待状态。
        """

        assignment = self.select_failover_assignment(
            shard=shard,
            requirements=requirements,
            candidates=candidates,
            attempts=attempts,
        )
        return assignment.node if assignment is not None else None

    def select_failover_assignment(
        self,
        *,
        shard: RunShard,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        attempts: Iterable[ShardAttempt],
    ) -> ResourceAssignment | None:
        """选择 failover 节点及完整空闲资源集合，排除历史尝试节点。"""

        attempted_nodes = {
            attempt.node_id
            for attempt in attempts
            if attempt.node_id
        }
        return self._select_assignment_excluding_nodes(
            shard=shard,
            requirements=requirements,
            candidates=candidates,
            excluded_node_ids=attempted_nodes,
        )

    def supports_resources(
        self, node: Node, requirements: tuple[DeviceRequirement, ...]
    ) -> bool:
        """判断节点是否具备资源能力，忽略当前 online/BUSY 占用状态。"""

        return self._allocate_devices(
            node,
            requirements,
            frozenset(),
            include_occupied=True,
        ) is not None

    def _select_assignment_excluding_nodes(
        self,
        *,
        shard: RunShard,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        excluded_node_ids: Iterable[str],
    ) -> ResourceAssignment | None:
        excluded = frozenset(excluded_node_ids)
        for state in self.eligible_candidates(
            shard=shard,
            requirements=requirements,
            candidates=candidates,
            excluded_node_ids=excluded,
        ):
            devices = self._allocate_devices(
                state.node,
                requirements.devices,
                state.reserved_device_ids,
            )
            if devices is not None:
                return ResourceAssignment(node=state.node, devices=devices)
        return None

    @staticmethod
    def reserve_devices(
        state: NodeSchedulingState, device_ids: Iterable[str]
    ) -> NodeSchedulingState:
        """为本轮已选节点预留一个 Shard 的完整设备集合。"""

        available = set(_available_device_ids(state.node, state.reserved_device_ids))
        requested = frozenset(device_ids)
        if not requested.issubset(available):
            raise ValueError(
                f"设备不可预留: node={state.node.node_id} devices={sorted(requested)}"
            )
        return NodeSchedulingState(
            node=state.node,
            reserved_device_ids=state.reserved_device_ids | requested,
        )

    def _allocate_devices(
        self,
        node: Node,
        requirements: tuple[DeviceRequirement, ...],
        reserved_device_ids: frozenset[str],
        *,
        include_occupied: bool = False,
    ) -> tuple[Device, ...] | None:
        if not requirements:
            return ()
        available = tuple(
            device
            for device in node.devices
            if device.device_id not in reserved_device_ids
            and (
                include_occupied
                or (
                    device.online
                    and device.status is not DeviceStatus.BUSY
                )
            )
        )
        ordered_requirements = tuple(
            sorted(requirements, key=_requirement_specificity, reverse=True)
        )
        return self._allocate_requirement_groups(
            available, ordered_requirements, index=0, used=frozenset()
        )

    def _allocate_requirement_groups(
        self,
        available: tuple[Device, ...],
        requirements: tuple[DeviceRequirement, ...],
        *,
        index: int,
        used: frozenset[str],
    ) -> tuple[Device, ...] | None:
        if index == len(requirements):
            return ()
        requirement = requirements[index]
        matches = tuple(
            device
            for device in available
            if device.device_id not in used
            and self._device_matcher.matches(
                device.capability, requirement
            )
        )
        for selected in combinations(matches, requirement.quantity):
            selected_ids = frozenset(device.device_id for device in selected)
            if requirement.device_ids and not set(requirement.device_ids).issubset(
                selected_ids
            ):
                continue
            remainder = self._allocate_requirement_groups(
                available,
                requirements,
                index=index + 1,
                used=used | selected_ids,
            )
            if remainder is not None:
                return selected + remainder
        return None

    @staticmethod
    def _priority_key(state: NodeSchedulingState) -> tuple[float, str]:
        """设备可用后按最近心跳和业务 ID 稳定排序。"""

        node = state.node
        last_seen = _timestamp(node.last_seen_at)
        return (-last_seen, node.node_id)


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
    return (len(requirement.device_ids) * 100 + constrained_fields, requirement.quantity)


def _timestamp(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    return value.timestamp()
