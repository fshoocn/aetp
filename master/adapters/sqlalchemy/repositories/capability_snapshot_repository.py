"""M2 节点能力和诊断快照 SQLAlchemy 仓储。"""

from __future__ import annotations

from aetp_protocol.capabilities import NodeCapabilitySnapshot
from aetp_protocol.ids import BusinessId, RequestId, SessionId, Sha256
from aetp_protocol.payloads import DiagnosticsSnapshot
from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import (
    AgentDiagnosticsSnapshot as DiagnosticsORM,
)
from master.adapters.sqlalchemy.orm import (
    NodeCapabilitySnapshot as CapabilityORM,
)
from master.domain.models import AgentDiagnosticsSnapshotRecord, NodeCapabilitySnapshotRecord
from master.domain.repositories import (
    AgentDiagnosticsSnapshotRepository,
    NodeCapabilitySnapshotRepository,
)


def _capability_to_domain(orm: CapabilityORM) -> NodeCapabilitySnapshotRecord:
    return NodeCapabilitySnapshotRecord(
        id=orm.id,
        node_id=BusinessId(orm.node_id),
        session_id=SessionId(orm.session_id),
        revision=orm.revision,
        snapshot_sha256=Sha256(orm.snapshot_sha256),
        snapshot=NodeCapabilitySnapshot.model_validate(orm.snapshot),
        reported_at=orm.reported_at,
        created_at=orm.created_at,
    )


def _diagnostics_to_domain(orm: DiagnosticsORM) -> AgentDiagnosticsSnapshotRecord:
    return AgentDiagnosticsSnapshotRecord(
        id=orm.id,
        request_id=RequestId(orm.request_id),
        node_id=BusinessId(orm.node_id),
        session_id=SessionId(orm.session_id),
        snapshot=DiagnosticsSnapshot.model_validate(orm.snapshot),
        collected_at=orm.collected_at,
        created_at=orm.created_at,
    )


class NodeCapabilitySnapshotRepositoryImpl(NodeCapabilitySnapshotRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_latest(self, node_id: BusinessId) -> NodeCapabilitySnapshotRecord | None:
        orm = self._s.execute(
            select(CapabilityORM)
            .where(CapabilityORM.node_id == node_id.root)
            .order_by(CapabilityORM.created_at.desc(), CapabilityORM.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _capability_to_domain(orm) if orm is not None else None

    def list_by_node(
        self,
        node_id: BusinessId,
        *,
        limit: int = 100,
    ) -> list[NodeCapabilitySnapshotRecord]:
        if limit < 1:
            raise ValueError("能力快照查询 limit 必须大于 0")
        statement = (
            select(CapabilityORM)
            .where(CapabilityORM.node_id == node_id.root)
            .order_by(CapabilityORM.created_at.desc(), CapabilityORM.id.desc())
            .limit(limit)
        )
        return [_capability_to_domain(item) for item in self._s.execute(statement).scalars().all()]

    def add_if_newer(self, record: NodeCapabilitySnapshotRecord) -> bool:
        latest = self.get_latest(record.node_id)
        if latest is not None and latest.session_id == record.session_id and record.revision <= latest.revision:
            if record.revision == latest.revision and record.snapshot_sha256 != latest.snapshot_sha256:
                raise ValueError("同一节点 session/revision 的能力快照摘要冲突")
            return False
        orm = CapabilityORM(
            node_id=record.node_id.root,
            session_id=record.session_id.root,
            revision=record.revision,
            snapshot_sha256=record.snapshot_sha256.root,
            snapshot=record.snapshot.model_dump(mode="json"),
            reported_at=record.reported_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return True


class AgentDiagnosticsSnapshotRepositoryImpl(AgentDiagnosticsSnapshotRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_latest(self, node_id: BusinessId) -> AgentDiagnosticsSnapshotRecord | None:
        orm = self._s.execute(
            select(DiagnosticsORM)
            .where(DiagnosticsORM.node_id == node_id.root)
            .order_by(DiagnosticsORM.collected_at.desc(), DiagnosticsORM.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _diagnostics_to_domain(orm) if orm is not None else None

    def get_by_request_id(self, request_id: RequestId) -> AgentDiagnosticsSnapshotRecord | None:
        orm = self._s.execute(
            select(DiagnosticsORM).where(DiagnosticsORM.request_id == request_id.root)
        ).scalar_one_or_none()
        return _diagnostics_to_domain(orm) if orm is not None else None

    def add(self, record: AgentDiagnosticsSnapshotRecord) -> AgentDiagnosticsSnapshotRecord:
        existing = self.get_by_request_id(record.request_id)
        if existing is not None:
            if (
                existing.node_id != record.node_id
                or existing.session_id != record.session_id
                or existing.snapshot != record.snapshot
            ):
                raise ValueError("诊断 request_id 已用于不同快照")
            return existing
        orm = DiagnosticsORM(
            request_id=record.request_id.root,
            node_id=record.node_id.root,
            session_id=record.session_id.root,
            snapshot=record.snapshot.model_dump(mode="json"),
            collected_at=record.collected_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _diagnostics_to_domain(orm)
