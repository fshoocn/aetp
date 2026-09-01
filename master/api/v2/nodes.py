"""AETP V2 节点能力和诊断查询 API。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from aetp_protocol.capabilities import NodeCapabilitySnapshot
from aetp_protocol.ids import BusinessId, RequestId, SessionId, Sha256
from aetp_protocol.payloads import DiagnosticsSnapshot
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from master.api.v1.dependencies import CurrentUser
from master.application.services.capability_snapshot_service import (
    CapabilitySnapshotProjectionService,
    DiagnosticsSnapshotProjectionService,
)
from master.application.services.diagnostics_request_service import (
    AgentOfflineForDiagnostics,
    DiagnosticsRequestService,
)
from master.domain.models import (
    AgentDiagnosticsSnapshotRecord,
    NodeCapabilitySnapshotRecord,
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


def get_capability_service(request: Request) -> CapabilitySnapshotProjectionService:
    return request.app.state.container.capability_snapshot_service()


def get_diagnostics_service(request: Request) -> DiagnosticsSnapshotProjectionService:
    return request.app.state.container.diagnostics_snapshot_service()


def get_diagnostics_request_service(request: Request) -> DiagnosticsRequestService:
    return request.app.state.container.diagnostics_request_service()


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
