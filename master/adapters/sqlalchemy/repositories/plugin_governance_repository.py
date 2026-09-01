"""V2 插件治理 SQLAlchemy 仓储。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, PluginId, SemVer, SessionId, Sha256
from aetp_protocol.plugin_types import DesiredPluginVersion, PluginPoint, PluginStatus
from aetp_protocol.plugins import PluginManifest, PluginSyncItem, PluginSyncItemResult
from master.adapters.sqlalchemy.orm import (
    AgentPluginDesiredVersion as DesiredORM,
    AgentPluginSyncOperation as SyncORM,
    PluginVersion as PluginVersionORM,
)
from master.domain.models import (
    AgentPluginDesiredVersionRecord,
    AgentPluginSyncOperationRecord,
    PluginSyncOperationState,
    PluginVersionRecord,
)
from master.domain.repositories import (
    AgentPluginDesiredVersionRepository,
    AgentPluginSyncOperationRepository,
    PluginVersionRepository,
)


def _plugin_to_domain(orm: PluginVersionORM) -> PluginVersionRecord:
    return PluginVersionRecord(
        id=orm.id,
        plugin_id=PluginId(orm.plugin_id),
        version=SemVer(orm.version),
        point=PluginPoint(orm.point),
        status=PluginStatus(orm.status),
        filename=orm.filename,
        archive_sha256=Sha256(orm.archive_sha256),
        manifest_sha256=Sha256(orm.manifest_sha256),
        manifest=PluginManifest.model_validate(orm.manifest),
        archive_path=orm.archive_path,
        installed_at=orm.installed_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _desired_to_domain(orm: DesiredORM) -> AgentPluginDesiredVersionRecord:
    return AgentPluginDesiredVersionRecord(
        id=orm.id,
        node_id=BusinessId(orm.node_id),
        desired=DesiredPluginVersion(
            plugin_id=PluginId(orm.plugin_id),
            point=PluginPoint(orm.point),
            version=SemVer(orm.version),
            auto_update=orm.auto_update,
            maintenance_window=orm.maintenance_window,
        ),
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _sync_to_domain(orm: SyncORM) -> AgentPluginSyncOperationRecord:
    return AgentPluginSyncOperationRecord(
        id=orm.id,
        sync_id=BusinessId(orm.sync_id),
        node_id=BusinessId(orm.node_id),
        expected_session_id=SessionId(orm.expected_session_id),
        state=PluginSyncOperationState(orm.state),
        items=tuple(PluginSyncItem.model_validate(item) for item in orm.items),
        results=(
            tuple(PluginSyncItemResult.model_validate(result) for result in orm.results)
            if orm.results is not None
            else None
        ),
        accepted=orm.accepted,
        restart_required=orm.restart_required,
        error_code=ErrorCode(orm.error_code) if orm.error_code is not None else None,
        completed_at=orm.completed_at,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class PluginVersionRepositoryImpl(PluginVersionRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, plugin_id: PluginId, version: SemVer) -> PluginVersionRecord | None:
        orm = self._s.execute(
            select(PluginVersionORM).where(
                PluginVersionORM.plugin_id == plugin_id.root,
                PluginVersionORM.version == version.root,
            )
        ).scalar_one_or_none()
        return _plugin_to_domain(orm) if orm is not None else None

    def get_by_archive_sha256(self, archive_sha256: Sha256) -> PluginVersionRecord | None:
        orm = self._s.execute(
            select(PluginVersionORM).where(PluginVersionORM.archive_sha256 == archive_sha256.root)
        ).scalar_one_or_none()
        return _plugin_to_domain(orm) if orm is not None else None

    def list(
        self,
        *,
        point: PluginPoint | None = None,
        status: PluginStatus | None = None,
    ) -> list[PluginVersionRecord]:
        stmt = select(PluginVersionORM).order_by(PluginVersionORM.plugin_id, PluginVersionORM.version)
        if point is not None:
            stmt = stmt.where(PluginVersionORM.point == point.value)
        if status is not None:
            stmt = stmt.where(PluginVersionORM.status == status.value)
        return [_plugin_to_domain(item) for item in self._s.execute(stmt).scalars().all()]

    def add(self, record: PluginVersionRecord) -> PluginVersionRecord:
        orm = PluginVersionORM(
            plugin_id=record.plugin_id.root,
            version=record.version.root,
            point=record.point.value,
            status=record.status.value,
            filename=record.filename,
            archive_sha256=record.archive_sha256.root,
            manifest_sha256=record.manifest_sha256.root,
            manifest=record.manifest.model_dump(mode="json", exclude_none=True),
            archive_path=record.archive_path,
            installed_at=record.installed_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _plugin_to_domain(orm)

    def update(self, record: PluginVersionRecord) -> PluginVersionRecord:
        if record.id is None:
            raise ValueError("更新插件版本记录必须包含 id")
        orm = self._s.get(PluginVersionORM, record.id)
        if orm is None:
            raise KeyError(f"插件版本记录不存在: {record.plugin_id.root}@{record.version.root}")
        orm.status = record.status.value
        orm.filename = record.filename
        orm.archive_path = record.archive_path
        orm.installed_at = record.installed_at
        self._s.flush()
        self._s.refresh(orm)
        return _plugin_to_domain(orm)


class AgentPluginDesiredVersionRepositoryImpl(AgentPluginDesiredVersionRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, node_id: BusinessId, plugin_id: PluginId) -> AgentPluginDesiredVersionRecord | None:
        orm = self._s.execute(
            select(DesiredORM).where(
                DesiredORM.node_id == node_id.root,
                DesiredORM.plugin_id == plugin_id.root,
            )
        ).scalar_one_or_none()
        return _desired_to_domain(orm) if orm is not None else None

    def list_by_node(self, node_id: BusinessId) -> list[AgentPluginDesiredVersionRecord]:
        stmt = select(DesiredORM).where(DesiredORM.node_id == node_id.root).order_by(DesiredORM.plugin_id)
        return [_desired_to_domain(item) for item in self._s.execute(stmt).scalars().all()]

    def upsert(self, record: AgentPluginDesiredVersionRecord) -> AgentPluginDesiredVersionRecord:
        orm = self._s.execute(
            select(DesiredORM).where(
                DesiredORM.node_id == record.node_id.root,
                DesiredORM.plugin_id == record.desired.plugin_id.root,
            )
        ).scalar_one_or_none()
        if orm is None:
            orm = DesiredORM(
                node_id=record.node_id.root,
                plugin_id=record.desired.plugin_id.root,
            )
            self._s.add(orm)
        orm.point = record.desired.point.value
        orm.version = record.desired.version.root
        orm.auto_update = record.desired.auto_update
        orm.maintenance_window = record.desired.maintenance_window
        self._s.flush()
        self._s.refresh(orm)
        return _desired_to_domain(orm)

    def remove(self, node_id: BusinessId, plugin_id: PluginId) -> None:
        orm = self._s.execute(
            select(DesiredORM).where(
                DesiredORM.node_id == node_id.root,
                DesiredORM.plugin_id == plugin_id.root,
            )
        ).scalar_one_or_none()
        if orm is not None:
            self._s.delete(orm)
            self._s.flush()


class AgentPluginSyncOperationRepositoryImpl(AgentPluginSyncOperationRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, sync_id: BusinessId) -> AgentPluginSyncOperationRecord | None:
        orm = self._s.execute(
            select(SyncORM).where(SyncORM.sync_id == sync_id.root)
        ).scalar_one_or_none()
        return _sync_to_domain(orm) if orm is not None else None

    def list_by_node(self, node_id: BusinessId) -> list[AgentPluginSyncOperationRecord]:
        stmt = select(SyncORM).where(SyncORM.node_id == node_id.root).order_by(SyncORM.created_at)
        return [_sync_to_domain(item) for item in self._s.execute(stmt).scalars().all()]

    def add(self, record: AgentPluginSyncOperationRecord) -> AgentPluginSyncOperationRecord:
        orm = SyncORM(
            sync_id=record.sync_id.root,
            node_id=record.node_id.root,
            expected_session_id=record.expected_session_id.root,
            state=record.state.value,
            items=[item.model_dump(mode="json", exclude_none=True) for item in record.items],
            results=(
                [result.model_dump(mode="json", exclude_none=True) for result in record.results]
                if record.results is not None
                else None
            ),
            accepted=record.accepted,
            restart_required=record.restart_required,
            error_code=record.error_code.root if record.error_code is not None else None,
            completed_at=record.completed_at,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _sync_to_domain(orm)

    def update(self, record: AgentPluginSyncOperationRecord) -> AgentPluginSyncOperationRecord:
        if record.id is None:
            raise ValueError("更新同步操作必须包含 id")
        orm = self._s.get(SyncORM, record.id)
        if orm is None:
            raise KeyError(f"同步操作不存在: {record.sync_id.root}")
        orm.state = record.state.value
        orm.results = (
            [result.model_dump(mode="json", exclude_none=True) for result in record.results]
            if record.results is not None
            else None
        )
        orm.accepted = record.accepted
        orm.restart_required = record.restart_required
        orm.error_code = record.error_code.root if record.error_code is not None else None
        orm.completed_at = record.completed_at
        self._s.flush()
        self._s.refresh(orm)
        return _sync_to_domain(orm)
