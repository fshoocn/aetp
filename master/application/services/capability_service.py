"""硬件能力匹配服务（P4.5，§18.5 / D-23）。

薄封装纯领域匹配器（domain/capability.py），面向调用方（触发 Run 硬校验、
调度器 P4.6 选节点）提供两个语义：
- require_node：硬校验——不满足抛 NodeCapabilityMismatchError
  （NODE_CAPABILITY_MISMATCH，§5.5，D-23 触发时硬校验）
- filter_candidates：候选节点过滤（§18.5：∩ 谓词满足）
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from aetp_protocol.capabilities import HardwareRequirements

from master.application.errors import NodeCapabilityMismatchError
from master.domain.capability import (
    CapabilityEvaluator,
    CapabilityMatch,
    list_capability_paths,
)
from master.domain.models import Node

logger = logging.getLogger(__name__)


class CapabilityService:
    """硬件能力匹配服务（无状态，纯函数包装）。"""

    def __init__(self, evaluator: CapabilityEvaluator | None = None) -> None:
        self._evaluator = evaluator or CapabilityEvaluator()

    def evaluate(
        self,
        node: Node,
        requirements: HardwareRequirements,
    ) -> CapabilityMatch:
        """求值单个节点是否满足硬件需求（不抛错，返回匹配结果）。"""
        return self._evaluator.evaluate(node.capabilities, requirements, node.tags)

    def require_node(
        self,
        node: Node,
        requirements: HardwareRequirements,
    ) -> None:
        """硬校验：节点不满足硬件需求时抛 NodeCapabilityMismatchError。"""
        match = self.evaluate(node, requirements)
        if not match.matched:
            available = list_capability_paths(node.capabilities)
            logger.warning(
                "节点能力不匹配: node=%s failures=%s available=%s",
                node.node_id,
                list(match.failures),
                available,
            )
            raise NodeCapabilityMismatchError(
                node.node_id,
                match.failures,
                available=available,
            )

    def filter_candidates(
        self,
        nodes: Iterable[Node],
        requirements: HardwareRequirements,
    ) -> list[Node]:
        """返回满足硬件需求的节点子集（§18.5 候选节点 ∩ 谓词满足）。"""
        return [
            node
            for node in nodes
            if self.evaluate(node, requirements).matched
        ]
