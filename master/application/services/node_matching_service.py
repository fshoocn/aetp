"""Master V2 节点匹配应用服务。"""

from __future__ import annotations

from collections.abc import Callable

from aetp_protocol.errors import ErrorCode
from aetp_protocol.execution import ExecutionRequirement
from aetp_protocol.ids import BusinessId

from master.application.services.capability_snapshot_service import (
    CapabilitySnapshotProjectionService,
)
from master.domain.node_matcher import NodeCapabilityCandidate, NodeMatch, NodeMatcher
from master.domain.repositories import UnitOfWork


class NodeMatchingService:
    """用最新有效能力快照评估节点，不申请资源或修改调度状态。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        capability_snapshots: CapabilitySnapshotProjectionService,
        matcher: NodeMatcher | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._capability_snapshots = capability_snapshots
        self._matcher = matcher or NodeMatcher()

    def match(
        self,
        requirement: ExecutionRequirement,
        *,
        node_ids: tuple[BusinessId, ...] = (),
    ) -> tuple[NodeMatch, ...]:
        allowed = {node_id.root for node_id in node_ids}
        with self._uow_factory() as uow:
            nodes = uow.nodes.list_all()

        candidates: list[NodeCapabilityCandidate] = []
        missing_snapshots: list[NodeMatch] = []
        for node in nodes:
            if allowed and node.node_id not in allowed:
                continue
            try:
                node_id = BusinessId(node.node_id)
            except ValueError:
                continue
            snapshot = self._capability_snapshots.latest(node_id)
            if snapshot is None:
                failures = [ErrorCode("NODE_CAPABILITY_MISMATCH")]
                if not node.online:
                    failures.append(ErrorCode("AGENT_OFFLINE"))
                missing_snapshots.append(
                    NodeMatch(
                        node_id=node_id,
                        matched=False,
                        failures=tuple(failures),
                    )
                )
                continue
            candidates.append(
                NodeCapabilityCandidate(
                    snapshot=snapshot.snapshot,
                    online=node.online,
                    enabled=node.enabled,
                )
            )
        return self._matcher.match(tuple(candidates), requirement) + tuple(missing_snapshots)
