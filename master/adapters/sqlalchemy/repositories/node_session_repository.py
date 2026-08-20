"""SQLAlchemy 节点会话仓储实现（P4.4，node_sessions 表）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import NodeSession as NodeSessionORM
from master.domain.enums import DisconnectReason
from master.domain.models import NodeSession
from master.domain.repositories import NodeSessionRepository


def _to_domain(orm: NodeSessionORM) -> NodeSession:
    return NodeSession(
        id=orm.id,
        node_pk=orm.node_pk,
        node_id=orm.node_id,
        session_id=orm.session_id,
        client_id=orm.client_id,
        connected_at=orm.connected_at,
        disconnected_at=orm.disconnected_at,
        disconnect_reason=(
            DisconnectReason(orm.disconnect_reason)
            if orm.disconnect_reason is not None
            else None
        ),
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class NodeSessionRepositoryImpl(NodeSessionRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, node_pk: int, session_id: str) -> NodeSession | None:
        orm = self._s.execute(
            select(NodeSessionORM).where(
                NodeSessionORM.node_pk == node_pk,
                NodeSessionORM.session_id == session_id,
            )
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def get_current(self, node_pk: int) -> NodeSession | None:
        orm = self._s.execute(
            select(NodeSessionORM)
            .where(
                NodeSessionORM.node_pk == node_pk,
                NodeSessionORM.disconnected_at.is_(None),
            )
            .order_by(NodeSessionORM.id.desc())
        ).scalars().first()
        return _to_domain(orm) if orm is not None else None

    def create(self, session: NodeSession) -> NodeSession:
        orm = NodeSessionORM(
            node_pk=session.node_pk,
            node_id=session.node_id,
            session_id=session.session_id,
            client_id=session.client_id,
            connected_at=session.connected_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def close(
        self,
        session: NodeSession,
        *,
        reason: DisconnectReason,
        at: datetime | None = None,
    ) -> NodeSession:
        orm = self._s.get(NodeSessionORM, session.id)
        if orm is None:
            raise ValueError(f"Node 会话不存在: id={session.id}")
        orm.disconnected_at = at
        orm.disconnect_reason = reason.value
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def close_all_open(self, *, reason: DisconnectReason, at: datetime | None = None) -> int:
        """关闭所有未关闭的会话（Master 启动恢复用：掉线期间会话已失效）。

        Returns:
            关闭的会话数量
        """
        from sqlalchemy import update as sa_update

        from typing import Any
        from sqlalchemy.engine import Result
        result: Result[Any] = self._s.execute(
            sa_update(NodeSessionORM)
            .where(NodeSessionORM.disconnected_at.is_(None))
            .values(disconnected_at=at, disconnect_reason=reason.value)
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            self._s.flush()
        return count
