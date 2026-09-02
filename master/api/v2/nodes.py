"""AETP V2 节点能力和诊断查询 API。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Annotated, Literal, cast

from aetp_protocol.capabilities import NodeCapabilitySnapshot
from aetp_protocol.errors import ErrorCode
from aetp_protocol.ids import BusinessId, PluginId, RequestId, SessionId, Sha256
from aetp_protocol.logs import LogEvent, LogLevel
from aetp_protocol.payloads import DiagnosticsSnapshot, RemoteOperationStatus
from aetp_protocol.plugin_types import DesiredPluginVersion
from aetp_protocol.plugins import PluginSyncItem, PluginSyncItemResult
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse

from master.adapters.sse.event_bus import EventBus
from master.api.v1.dependencies import CurrentUser, UowFactoryDep
from master.api.v1.permissions import PlatformAdminDep
from master.application.services.agent_log_service import AgentLogService
from master.application.services.agent_maintenance_service import (
    AgentMaintenanceService,
    AgentOfflineForMaintenance,
    MaintenanceLockConflict,
)
from master.application.services.capability_snapshot_service import (
    CapabilitySnapshotProjectionService,
    DiagnosticsSnapshotProjectionService,
)
from master.application.services.diagnostics_request_service import (
    AgentOfflineForDiagnostics,
    DiagnosticsRequestService,
)
from master.application.services.plugin_sync_service import (
    AgentOfflineForPluginSync,
    PluginSyncService,
)
from master.domain.models import (
    AgentDiagnosticsSnapshotRecord,
    AgentLogEventRecord,
    AgentPluginSyncOperationRecord,
    NodeCapabilitySnapshotRecord,
    PluginSyncOperationState,
    RemoteOperationRecord,
)

router = APIRouter(prefix="/api/v2/nodes", tags=["v2-nodes"])


class CapabilitySnapshotView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: BusinessId
    session_id: SessionId
    revision: int
    snapshot_sha256: Sha256
    snapshot: NodeCapabilitySnapshot
    reported_at: datetime
    created_at: datetime | None


class DiagnosticsSnapshotView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    node_id: BusinessId
    session_id: SessionId
    snapshot: DiagnosticsSnapshot
    collected_at: datetime
    created_at: datetime | None


class DiagnosticsCollectView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: BusinessId
    request_id: RequestId
    node_id: BusinessId
    status: Literal["pending"]


class AgentLogView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: BusinessId
    session_id: SessionId
    sequence: int = Field(ge=1)
    event: LogEvent
    batch_first_sequence: int = Field(ge=1)
    received_at: datetime


class RemoteOperationView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: BusinessId
    node_id: BusinessId
    kind: Literal["diagnostics", "plugin_sync", "log_level", "drain", "restart"]
    status: RemoteOperationStatus
    expected_session_id: SessionId | None
    request: dict[str, object]
    error_code: ErrorCode | None
    message: str
    created_at: datetime | None
    updated_at: datetime | None


RemoteOperationKind = Literal["diagnostics", "plugin_sync", "log_level", "drain", "restart"]


class LogLevelUpdateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(min_length=1, max_length=128)
    plugin_id: PluginId | None = None
    level: LogLevel
    expires_at: datetime | None = None


class MaintenanceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    drain_timeout_s: int = Field(default=1800, ge=0)
    reason: str = Field(default="", max_length=1024)


class PluginSyncCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[PluginSyncItem, ...] = Field(min_length=1)
    drain_timeout_s: int = Field(default=1800, ge=0)
    restart_after: bool = True


class PluginSyncView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sync_id: BusinessId
    node_id: BusinessId
    expected_session_id: SessionId
    state: PluginSyncOperationState
    items: tuple[PluginSyncItem, ...]
    results: tuple[PluginSyncItemResult, ...] | None
    accepted: bool | None
    restart_required: bool
    completed_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


class DesiredPluginVersionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: BusinessId
    desired: DesiredPluginVersion


class V2NodeView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: BusinessId
    name: str
    hostname: str
    status: str
    online: bool
    enabled: bool
    tags: tuple[str, ...]
    protocol_version: str
    last_seen_at: datetime | None
    load: dict[str, object]
    resource_occupancy: dict[str, str]


def get_capability_service(request: Request) -> CapabilitySnapshotProjectionService:
    return request.app.state.container.capability_snapshot_service()


def get_diagnostics_service(request: Request) -> DiagnosticsSnapshotProjectionService:
    return request.app.state.container.diagnostics_snapshot_service()


def get_diagnostics_request_service(request: Request) -> DiagnosticsRequestService:
    return request.app.state.container.diagnostics_request_service()


def get_plugin_sync_service(request: Request) -> PluginSyncService:
    return request.app.state.container.plugin_sync_service()


def get_agent_log_service(request: Request) -> AgentLogService:
    return request.app.state.container.agent_log_service()


def get_agent_maintenance_service(request: Request) -> AgentMaintenanceService:
    return request.app.state.container.agent_maintenance_service()


def get_event_bus(request: Request) -> EventBus:
    return request.app.state.container.event_bus()


def _node_id(value: str) -> BusinessId:
    try:
        return BusinessId(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="节点 ID 不合法") from exc


def _capability_view(record: NodeCapabilitySnapshotRecord) -> CapabilitySnapshotView:
    return CapabilitySnapshotView(
        node_id=record.node_id,
        session_id=record.session_id,
        revision=record.revision,
        snapshot_sha256=record.snapshot_sha256,
        snapshot=record.snapshot,
        reported_at=record.reported_at,
        created_at=record.created_at,
    )


def _diagnostics_view(record: AgentDiagnosticsSnapshotRecord) -> DiagnosticsSnapshotView:
    return DiagnosticsSnapshotView(
        request_id=record.request_id,
        node_id=record.node_id,
        session_id=record.session_id,
        snapshot=record.snapshot,
        collected_at=record.collected_at,
        created_at=record.created_at,
    )


def _plugin_sync_view(record: AgentPluginSyncOperationRecord) -> PluginSyncView:
    return PluginSyncView(
        sync_id=record.sync_id,
        node_id=record.node_id,
        expected_session_id=record.expected_session_id,
        state=record.state,
        items=record.items,
        results=record.results,
        accepted=record.accepted,
        restart_required=record.restart_required,
        completed_at=record.completed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _agent_log_view(record: AgentLogEventRecord) -> AgentLogView:
    return AgentLogView(
        node_id=record.node_id,
        session_id=record.session_id,
        sequence=record.sequence,
        event=record.event,
        batch_first_sequence=record.batch_first_sequence,
        received_at=record.received_at,
    )


def _remote_operation_view(record: RemoteOperationRecord) -> RemoteOperationView:
    if record.kind not in {"diagnostics", "plugin_sync", "log_level", "drain", "restart"}:
        raise ValueError(f"未知远程操作类型: {record.kind}")
    return RemoteOperationView(
        operation_id=record.operation_id,
        node_id=record.node_id,
        kind=cast(RemoteOperationKind, record.kind),
        status=record.status,
        expected_session_id=record.expected_session_id,
        request=record.request,
        error_code=(ErrorCode(record.error_code) if record.error_code is not None else None),
        message=record.message,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _desired_view(record) -> DesiredPluginVersionView:
    return DesiredPluginVersionView(node_id=record.node_id, desired=record.desired)


def _node_view(node) -> V2NodeView:
    return V2NodeView(
        node_id=BusinessId(node.node_id),
        name=node.name,
        hostname=node.hostname,
        status=node.status.value,
        online=node.online,
        enabled=node.enabled,
        tags=tuple(node.tags or ()),
        protocol_version=node.protocol_version,
        last_seen_at=node.last_seen_at,
        load=dict(node.load or {}),
        resource_occupancy=dict(node.resource_occupancy or {}),
    )


def _format_agent_log_sse(
    event_id: str,
    sequence: int | None,
    data: dict[str, object],
    occurred_at: str,
) -> str:
    payload = json.dumps(
        {
            "event_id": event_id,
            "sequence": sequence,
            "node_id": data.get("source_id"),
            "type": "agent.log",
            "data": data,
            "ts": occurred_at,
        },
        ensure_ascii=False,
    )
    return f"id: {sequence or event_id}\nevent: agent.log\ndata: {payload}\n\n"


@router.get(
    "",
    response_model=list[V2NodeView],
)
def list_v2_nodes(
    _current_user: CurrentUser,
    uow_factory: UowFactoryDep,
    online: bool | None = None,
    enabled: bool | None = None,
) -> list[V2NodeView]:
    with uow_factory() as uow:
        nodes = uow.nodes.list_all(online=online, enabled=enabled)
    return [_node_view(node) for node in nodes]


@router.get(
    "/{node_id}/capability-snapshot",
    response_model=CapabilitySnapshotView,
)
def latest_capability_snapshot(
    node_id: str,
    _current_user: CurrentUser,
    service: Annotated[CapabilitySnapshotProjectionService, Depends(get_capability_service)],
) -> CapabilitySnapshotView:
    record = service.latest(_node_id(node_id))
    if record is None:
        raise HTTPException(status_code=404, detail="节点尚无能力快照")
    return _capability_view(record)


@router.get(
    "/{node_id}/capability-snapshots",
    response_model=list[CapabilitySnapshotView],
)
def capability_snapshot_history(
    node_id: str,
    _current_user: CurrentUser,
    service: Annotated[CapabilitySnapshotProjectionService, Depends(get_capability_service)],
    limit: int = 20,
) -> list[CapabilitySnapshotView]:
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit 必须在 1..100 范围内")
    return [_capability_view(item) for item in service.history(_node_id(node_id), limit=limit)]


@router.get(
    "/{node_id}/diagnostics",
    response_model=DiagnosticsSnapshotView,
)
def latest_diagnostics_snapshot(
    node_id: str,
    _current_user: CurrentUser,
    service: Annotated[DiagnosticsSnapshotProjectionService, Depends(get_diagnostics_service)],
) -> DiagnosticsSnapshotView:
    record = service.latest(_node_id(node_id))
    if record is None:
        raise HTTPException(status_code=404, detail="节点尚无诊断快照")
    return _diagnostics_view(record)


@router.get(
    "/{node_id}/logs",
    response_model=list[AgentLogView],
)
def list_agent_logs(
    node_id: str,
    _current_user: CurrentUser,
    service: Annotated[AgentLogService, Depends(get_agent_log_service)],
    session_id: str | None = None,
    after_sequence: int = 0,
    limit: int = 100,
    level: LogLevel | None = None,
    component: str | None = None,
    event_code: str | None = None,
    run_id: str | None = None,
    attempt_id: str | None = None,
    plugin_id: str | None = None,
    keyword: str | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
) -> list[AgentLogView]:
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence 不能小于 0")
    if not 1 <= limit <= 1000:
        raise HTTPException(status_code=422, detail="limit 必须在 1..1000 范围内")
    node = _node_id(node_id)
    normalized_session = session_id
    if session_id is not None:
        try:
            normalized_session = SessionId(session_id).root
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="session_id 不合法") from exc
    normalized_plugin = plugin_id
    if plugin_id is not None:
        try:
            normalized_plugin = PluginId(plugin_id).root
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="plugin_id 不合法") from exc
    try:
        records = service.list_logs(
            node,
            session_id=normalized_session,
            after_sequence=after_sequence,
            limit=limit,
            level=level.value if level is not None else None,
            component=component,
            event_code=event_code,
            run_id=run_id,
            attempt_id=attempt_id,
            plugin_id=normalized_plugin,
            keyword=keyword,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return [_agent_log_view(record) for record in records]


@router.get(
    "/{node_id}/logs/stream",
)
async def stream_agent_logs(
    request: Request,
    node_id: str,
    _current_user: CurrentUser,
    service: Annotated[AgentLogService, Depends(get_agent_log_service)],
    event_bus: Annotated[EventBus, Depends(get_event_bus)],
) -> StreamingResponse:
    node = _node_id(node_id)
    raw_last_event_id = request.headers.get("last-event-id", "0")
    try:
        last_sequence = max(0, int(raw_last_event_id or "0"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Last-Event-ID 必须是非负整数序号",
        ) from exc
    if last_sequence < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID 必须是非负整数序号")
    try:
        service.list_logs(node, limit=1)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def event_stream():
        queue = await event_bus.subscribe(node.root)
        try:
            yield ": connected\n\n"
            replay = service.list_logs(node, after_sequence=last_sequence, limit=1000)
            current_sequence = last_sequence
            for record in replay:
                yield _format_agent_log_sse(
                    record.event.event_id.root,
                    record.sequence,
                    record.event.model_dump(mode="json"),
                    record.event.occurred_at.isoformat(),
                )
                current_sequence = max(current_sequence, record.sequence)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if event is None:
                        break
                    if event.sequence is not None and event.sequence <= current_sequence:
                        continue
                    yield _format_agent_log_sse(
                        event.event_id,
                        event.sequence,
                        event.data,
                        event.ts,
                    )
                    current_sequence = max(current_sequence, event.sequence or 0)
                except TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            await event_bus.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{node_id}/diagnostics/collect",
    response_model=DiagnosticsCollectView,
)
def collect_diagnostics(
    node_id: str,
    _current_user: CurrentUser,
    service: Annotated[DiagnosticsRequestService, Depends(get_diagnostics_request_service)],
) -> DiagnosticsCollectView:
    try:
        operation = service.request(_node_id(node_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentOfflineForDiagnostics as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DiagnosticsCollectView(
        operation_id=operation.operation_id,
        request_id=operation.request.request_id,
        node_id=operation.request.node_id,
        status="pending",
    )


@router.get(
    "/{node_id}/maintenance/operations",
    response_model=list[RemoteOperationView],
)
def list_maintenance_operations(
    node_id: str,
    _current_user: CurrentUser,
    service: Annotated[AgentMaintenanceService, Depends(get_agent_maintenance_service)],
    limit: int = 100,
) -> list[RemoteOperationView]:
    if not 1 <= limit <= 1000:
        raise HTTPException(status_code=422, detail="limit 必须在 1..1000 范围内")
    try:
        records = service.list_operations(_node_id(node_id), limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return [_remote_operation_view(record) for record in records]


@router.get(
    "/{node_id}/maintenance/operations/{operation_id}",
    response_model=RemoteOperationView,
)
def get_maintenance_operation(
    node_id: str,
    operation_id: str,
    _current_user: CurrentUser,
    service: Annotated[AgentMaintenanceService, Depends(get_agent_maintenance_service)],
) -> RemoteOperationView:
    node = _node_id(node_id)
    try:
        operation = service.get_operation(BusinessId(operation_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="operation_id 不合法") from exc
    if operation is None or operation.node_id != node:
        raise HTTPException(status_code=404, detail="远程操作不存在")
    return _remote_operation_view(operation)


@router.post(
    "/{node_id}/log-level",
    response_model=RemoteOperationView,
    status_code=202,
)
def request_log_level_update(
    node_id: str,
    _admin: PlatformAdminDep,
    body: LogLevelUpdateCreateRequest,
    service: Annotated[AgentMaintenanceService, Depends(get_agent_maintenance_service)],
) -> RemoteOperationView:
    try:
        operation = service.request_log_level(
            _node_id(node_id),
            component=body.component,
            level=body.level,
            plugin_id=body.plugin_id,
            expires_at=body.expires_at,
            actor_id=_admin.persisted_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentOfflineForMaintenance as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _remote_operation_view(operation)


@router.post(
    "/{node_id}/maintenance/drain",
    response_model=RemoteOperationView,
    status_code=202,
)
def request_maintenance_drain(
    node_id: str,
    _admin: PlatformAdminDep,
    body: MaintenanceCreateRequest,
    service: Annotated[AgentMaintenanceService, Depends(get_agent_maintenance_service)],
) -> RemoteOperationView:
    try:
        operation = service.request_drain(
            _node_id(node_id),
            drain_timeout_s=body.drain_timeout_s,
            reason=body.reason,
            actor_id=_admin.persisted_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentOfflineForMaintenance as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MaintenanceLockConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _remote_operation_view(operation)


@router.post(
    "/{node_id}/maintenance/restart",
    response_model=RemoteOperationView,
    status_code=202,
)
def request_maintenance_restart(
    node_id: str,
    _admin: PlatformAdminDep,
    body: MaintenanceCreateRequest,
    service: Annotated[AgentMaintenanceService, Depends(get_agent_maintenance_service)],
) -> RemoteOperationView:
    try:
        operation = service.request_restart(
            _node_id(node_id),
            drain_timeout_s=body.drain_timeout_s,
            reason=body.reason,
            actor_id=_admin.persisted_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentOfflineForMaintenance as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MaintenanceLockConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _remote_operation_view(operation)


@router.post(
    "/{node_id}/maintenance/sync",
    response_model=PluginSyncView,
    status_code=202,
)
@router.post(
    "/{node_id}/plugin-sync",
    response_model=PluginSyncView,
    status_code=202,
)
def request_plugin_sync(
    node_id: str,
    _admin: PlatformAdminDep,
    body: PluginSyncCreateRequest,
    service: Annotated[PluginSyncService, Depends(get_plugin_sync_service)],
) -> PluginSyncView:
    try:
        record = service.request(
            _node_id(node_id),
            body.items,
            drain_timeout_s=body.drain_timeout_s,
            restart_after=body.restart_after,
            actor_id=_admin.persisted_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentOfflineForPluginSync as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MaintenanceLockConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _plugin_sync_view(record)


@router.put(
    "/{node_id}/desired-plugin",
    response_model=DesiredPluginVersionView,
)
def set_node_desired_plugin(
    node_id: str,
    desired: DesiredPluginVersion,
    _admin: PlatformAdminDep,
    service: Annotated[PluginSyncService, Depends(get_plugin_sync_service)],
) -> DesiredPluginVersionView:
    try:
        record = service.set_desired_version(_node_id(node_id), desired)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _desired_view(record)


@router.put(
    "/groups/{tag}/desired-plugin",
    response_model=list[DesiredPluginVersionView],
)
def set_group_desired_plugin(
    tag: str,
    desired: DesiredPluginVersion,
    _admin: PlatformAdminDep,
    service: Annotated[PluginSyncService, Depends(get_plugin_sync_service)],
) -> list[DesiredPluginVersionView]:
    try:
        records = service.set_desired_version_for_tag(tag, desired)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return [_desired_view(record) for record in records]
