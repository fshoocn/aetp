"""AETP V2 插件治理 API。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from aetp_protocol.ids import PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import PluginPoint, PluginStatus
from aetp_protocol.plugins import PluginManifest
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict

from master.api.v1.dependencies import CurrentUser
from master.api.v1.permissions import PlatformAdminDep
from master.application.services.plugin_governance_service import PluginGovernanceService
from master.domain.models import PluginVersionRecord

router = APIRouter(prefix="/api/v2/plugins", tags=["v2-plugins"])


class PluginVersionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: PluginId
    version: SemVer
    point: PluginPoint
    status: PluginStatus
    filename: str
    archive_sha256: Sha256
    manifest_sha256: Sha256
    manifest: PluginManifest
    installed_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


def get_plugin_service(request: Request) -> PluginGovernanceService:
    return request.app.state.container.plugin_governance_service()


def _view(record: PluginVersionRecord) -> PluginVersionView:
    return PluginVersionView(
        plugin_id=record.plugin_id,
        version=record.version,
        point=record.point,
        status=record.status,
        filename=record.filename,
        archive_sha256=record.archive_sha256,
        manifest_sha256=record.manifest_sha256,
        manifest=record.manifest,
        installed_at=record.installed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _plugin_id(value: str) -> PluginId:
    try:
        return PluginId(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="插件 ID 不合法") from exc


def _version(value: str) -> SemVer:
    try:
        return SemVer(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="插件版本不合法") from exc


@router.get("", response_model=list[PluginVersionView])
def list_plugins(
    _current_user: CurrentUser,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    point: PluginPoint | None = None,
    plugin_status: PluginStatus | None = None,
) -> list[PluginVersionView]:
    records = service.list_versions(plugin_id=None)
    return [
        _view(item)
        for item in records
        if (point is None or item.point is point)
        and (plugin_status is None or item.status is plugin_status)
    ]


@router.post("", response_model=PluginVersionView, status_code=status.HTTP_201_CREATED)
async def upload_plugin(
    _admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    file: UploadFile = File(...),  # noqa: B008 - FastAPI 依赖注入惯用写法
) -> PluginVersionView:
    try:
        record = service.register_archive(file.filename or "plugin.zip", await file.read())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _view(record)


@router.post("/{plugin_id}/{version}/install", response_model=PluginVersionView)
def install_plugin(
    plugin_id: str,
    version: str,
    _admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
) -> PluginVersionView:
    try:
        return _view(service.install(_plugin_id(plugin_id), _version(version)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{plugin_id}/{version}/enable", response_model=PluginVersionView)
def enable_plugin(
    plugin_id: str,
    version: str,
    _admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
) -> PluginVersionView:
    try:
        return _view(service.request_enabled(_plugin_id(plugin_id), _version(version)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{plugin_id}/{version}/disable", response_model=PluginVersionView)
def disable_plugin(
    plugin_id: str,
    version: str,
    _admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
) -> PluginVersionView:
    try:
        return _view(service.request_disabled(_plugin_id(plugin_id), _version(version)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{plugin_id}/{version}", status_code=status.HTTP_204_NO_CONTENT)
def remove_plugin(
    plugin_id: str,
    version: str,
    _admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
) -> None:
    try:
        service.remove(_plugin_id(plugin_id), _version(version))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
