"""Shard 候选节点选择（P4.6 重构，§18.5/§18.6）。

调度编排只负责候选过滤、排序与 failover 排除；能力匹配委托
``CapabilityEvaluator``，资源分配委托 ``ResourceAllocator``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from aetp_protocol.capabilities import DeviceRequirement, HardwareRequirements

from master.domain.capability import CapabilityEvaluator
from master.domain.enums import NodeStatus, ShardAttemptStatus
from master.domain.models import Node, RunShard, ShardAttempt
from master.domain.resources import (
    NodeSchedulingState,
    ResourceAllocator,
    ResourceAssignment,
)

__all__ = ["ShardScheduler"]


class ShardScheduler:
    """纯函数式候选节点选择器（编排层）。"""

    def __init__(
        self,
        evaluator: CapabilityEvaluator | None = None,
        allocator: ResourceAllocator | None = None,
    ) -> None:
        self._evaluator = evaluator or CapabilityEvaluator()
        self._allocator = allocator or ResourceAllocator()

    def select_node(
        self,
        *,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        excluded_node_ids: Iterable[str] = (),
    ) -> Node | None:
        """选择一个能力满足且设备可用的节点；没有可用节点时返回 ``None``。"""

        assignment = self.select_assignment(
            requirements=requirements,
            candidates=candidates,
            excluded_node_ids=excluded_node_ids,
        )
        return assignment.node if assignment is not None else None

    def select_assignment(
        self,
        *,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        excluded_node_ids: Iterable[str] = (),
    ) -> ResourceAssignment | None:
        """选择节点及该 Shard 所需的全部空闲设备。"""

        eligible = self.eligible_candidates(
            requirements=requirements,
            candidates=candidates,
            excluded_node_ids=excluded_node_ids,
        )
        if not eligible:
            return None
        state = eligible[0]
        allocation = self._allocator.allocate(
            state.node,
            requirements.devices,
            state.reserved_device_ids,
        )
        if allocation is None:
            return None
        return ResourceAssignment(
            node=state.node,
            devices=allocation.devices,
            routes_by_device=allocation.routes_by_device,
        )

    def eligible_candidates(
        self,
        *,
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
            if not self._evaluator.evaluate(
                node.capabilities, requirements, node.tags
            ).matched:
                continue
            if self._allocator.allocate(
                node, requirements.devices, state.reserved_device_ids
            ) is None:
                continue
            eligible.append(state)

        eligible.sort(key=self._priority_key)
        return tuple(eligible)

    def select_failover_node(
        self,
        *,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        attempts: Iterable[ShardAttempt],
    ) -> Node | None:
        """为同一 Shard 选择 failover 节点。

        D-20 要求换节点时保留历史 Attempt，因此所有历史尝试过的节点都
        排除；若没有其它节点可用，返回 ``None``，由应用层保持等待状态。
        """

        assignment = self.select_failover_assignment(
            requirements=requirements,
            candidates=candidates,
            attempts=attempts,
        )
        return assignment.node if assignment is not None else None

    def select_failover_assignment(
        self,
        *,
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
            requirements=requirements,
            candidates=candidates,
            excluded_node_ids=attempted_nodes,
        )

    def supports_resources(
        self, node: Node, requirements: tuple[DeviceRequirement, ...]
    ) -> bool:
        """判断节点是否具备资源能力，忽略当前 online/BUSY 占用状态。"""

        return self._allocator.supports(node, requirements)

    def _select_assignment_excluding_nodes(
        self,
        *,
        requirements: HardwareRequirements,
        candidates: Iterable[NodeSchedulingState],
        excluded_node_ids: Iterable[str],
    ) -> ResourceAssignment | None:
        excluded = frozenset(excluded_node_ids)
        for state in self.eligible_candidates(
            requirements=requirements,
            candidates=candidates,
            excluded_node_ids=excluded,
        ):
            allocation = self._allocator.allocate(
                state.node,
                requirements.devices,
                state.reserved_device_ids,
            )
            if allocation is not None:
                return ResourceAssignment(
                    node=state.node,
                    devices=allocation.devices,
                    routes_by_device=allocation.routes_by_device,
                )
        return None

    def reserve_devices(
        self, state: NodeSchedulingState, device_ids: Iterable[str]
    ) -> NodeSchedulingState:
        """为本轮已选节点预留一个 Shard 的完整设备集合。"""

        return self._allocator.reserve(state, device_ids)

    @staticmethod
    def _priority_key(state: NodeSchedulingState) -> tuple[float, str]:
        """设备可用后按最近心跳和业务 ID 稳定排序。"""

        node = state.node
        last_seen = _timestamp(node.last_seen_at)
        return (-last_seen, node.node_id)


def _timestamp(value: datetime | None) -> float:
    if value is None:
        return float("-inf")
    return value.timestamp()
