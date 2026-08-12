"""Node 查询服务。

Node 和 Device 的运行状态属于平台资产视图，所有已激活用户可只读查询。
项目绑定只决定项目调度范围，不限制基础资产的只读可见性。
"""

from __future__ import annotations

import logging
from typing import Callable

from master.domain.models import Node
from master.domain.repositories import UnitOfWork

logger = logging.getLogger(__name__)


class NodeService:
    """负责 Node 及其 Device 的只读查询。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def list_all(
        self,
        *,
        online: bool | None = None,
        enabled: bool | None = None,
    ) -> list[Node]:
        """查询所有 Node，并加载其 Device 列表。"""
        with self._uow_factory() as uow:
            nodes = uow.nodes.list_all(online=online, enabled=enabled)
            logger.debug(
                "查询全局 Node 列表: online=%s, enabled=%s, count=%s",
                online,
                enabled,
                len(nodes),
            )
            return nodes

    def get_by_id(self, node_id: str) -> Node | None:
        """按 node_id 查询 Node，并加载其 Device 列表。"""
        with self._uow_factory() as uow:
            node = uow.nodes.get_by_id(node_id)
            logger.debug(
                "查询 Node 详情: node_id=%s, found=%s",
                node_id,
                node is not None,
            )
            return node
