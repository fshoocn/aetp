"""项目节点绑定业务服务。

Node 表示运行 Agent 的电脑或执行端，Device 表示该 Node 管理的外设。
项目绑定 Node 后，查询绑定时同时返回该 Node 下的 Device 列表。
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.exc import IntegrityError

from master.application.errors import (
    NodeAlreadyBoundError,
    NodeBindingNotFoundError,
    NodeDisabledError,
    NodeNotFoundError,
    ProjectNotFoundError,
)
from master.domain.models import ProjectNodeBinding, ProjectNodeBindingView
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)


class ProjectNodeBindingService:
    """负责项目节点绑定的查询、绑定、启停和解绑。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def list_bindings(self, project_id: str) -> list[ProjectNodeBindingView]:
        """列出项目的节点绑定记录。"""
        with self._uow_factory() as uow:
            self._require_project(uow, project_id)
            bindings = uow.bindings.list_with_nodes(project_id)
            logger.debug(
                "查询项目 Node 绑定: project_id=%s, count=%s",
                project_id,
                len(bindings),
            )
            return bindings

    def bind_node(
        self,
        project_id: str,
        *,
        node_id: str,
        assigned_by: int,
    ) -> ProjectNodeBindingView:
        """将现有节点绑定到项目。"""
        normalized_node_id = node_id.strip()
        with self._uow_factory() as uow:
            self._require_project(uow, project_id)
            node = uow.nodes.get_by_id(normalized_node_id)
            if node is None:
                raise NodeNotFoundError("节点不存在")
            if not node.enabled:
                raise NodeDisabledError("节点已禁用，不能绑定到项目")

            if uow.bindings.get(project_id, normalized_node_id) is not None:
                raise NodeAlreadyBoundError("节点已经绑定到项目")

            now = utcnow()
            binding = ProjectNodeBinding(
                id=None,
                project_id=project_id,
                node_id=normalized_node_id,
                enabled=True,
                assigned_by=assigned_by,
                created_at=now,
                updated_at=now,
            )
            try:
                uow.bindings.add(binding)
            except IntegrityError as exc:
                raise NodeAlreadyBoundError("节点已经绑定到项目") from exc
            logger.info(
                "项目 Node 绑定成功: project_id=%s, node_id=%s, assigned_by=%s",
                project_id,
                normalized_node_id,
                assigned_by,
            )
            return self._view_for(uow, project_id, normalized_node_id)

    def update_binding(
        self,
        project_id: str,
        node_id: str,
        *,
        enabled: bool,
        assigned_by: int,
    ) -> ProjectNodeBindingView:
        """启用或禁用项目节点绑定。"""
        with self._uow_factory() as uow:
            binding = uow.bindings.get(project_id, node_id)
            if binding is None:
                raise NodeBindingNotFoundError("项目节点绑定不存在")
            binding.enabled = enabled
            binding.assigned_by = assigned_by
            binding.updated_at = utcnow()
            uow.bindings.update(binding)
            logger.info(
                "项目 Node 绑定状态更新: project_id=%s, node_id=%s, enabled=%s, assigned_by=%s",
                project_id,
                node_id,
                enabled,
                assigned_by,
            )
            return self._view_for(uow, project_id, node_id)

    def remove_binding(self, project_id: str, node_id: str) -> None:
        """解除项目节点绑定。"""
        with self._uow_factory() as uow:
            binding = uow.bindings.get(project_id, node_id)
            if binding is None:
                raise NodeBindingNotFoundError("项目节点绑定不存在")
            uow.bindings.remove(binding)
            logger.info(
                "项目 Node 解绑成功: project_id=%s, node_id=%s",
                project_id,
                node_id,
            )

    # ---- 内部 ----

    @staticmethod
    def _require_project(uow: UnitOfWork, project_id: str) -> None:
        if uow.projects.get_by_project_id(project_id) is None:
            raise ProjectNotFoundError("项目不存在")

    @staticmethod
    def _view_for(
        uow: UnitOfWork, project_id: str, node_id: str
    ) -> ProjectNodeBindingView:
        for view in uow.bindings.list_with_nodes(project_id):
            if view.node_id == node_id:
                return view
        raise NodeBindingNotFoundError("项目节点绑定不存在")
