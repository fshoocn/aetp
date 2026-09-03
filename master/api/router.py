"""AETP  插件治理 API。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from aetp_protocol.ids import PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import PluginPoint, PluginStatus
from aetp_protocol.plugins import PluginManifest
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict

from master.api.dependencies import CurrentUser, IdempotencyServiceDep
from master.api.permissions import PlatformAdminDep
from master.application.services.plugin_governance_service import PluginGovernanceService
from master.domain.models import PluginVersionRecord

from .idempotency import complete as complete_idempotency
from .idempotency import release as release_idempotency
from .idempotency import reserve_or_replay

router = APIRouter(prefix="/api/v2/plugins", tags=["plugins"])


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


def _mutate_plugin(
    *,
    service: PluginGovernanceService,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None,
    scope: str,
    payload: dict[str, object],
    action: Callable[[], PluginVersionRecord],
) -> PluginVersionView:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=scope,
        payload=payload,
        response_model=PluginVersionView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        response = _view(action())
        complete_idempotency(
            idempotency,
            result.reservation,
            response,
            response_status=status.HTTP_200_OK,
        )
        return response
    except KeyError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


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
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    file: UploadFile = File(...),  # noqa: B008 - FastAPI 依赖注入惯用写法
) -> PluginVersionView:
    filename = file.filename or "plugin.zip"
    content = await file.read()
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"plugin.upload:{admin.persisted_id}",
        payload={"filename": filename, "sha256": hashlib.sha256(content).hexdigest()},
        response_model=PluginVersionView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        response = _view(service.register_archive(filename, content))
        complete_idempotency(
            idempotency,
            result.reservation,
            response,
            response_status=status.HTTP_201_CREATED,
        )
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.post("/{plugin_id}/{version}/install", response_model=PluginVersionView)
def install_plugin(
    plugin_id: str,
    version: str,
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PluginVersionView:
    typed_plugin_id = _plugin_id(plugin_id)
    typed_version = _version(version)
    return _mutate_plugin(
        service=service,
        idempotency=idempotency,
        idempotency_key=idempotency_key,
        scope=f"plugin.install:{admin.persisted_id}:{plugin_id}:{version}",
        payload={"plugin_id": plugin_id, "version": version, "operation": "install"},
        action=lambda: service.install(typed_plugin_id, typed_version),
    )


@router.post("/{plugin_id}/{version}/enable", response_model=PluginVersionView)
def enable_plugin(
    plugin_id: str,
    version: str,
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PluginVersionView:
    typed_plugin_id = _plugin_id(plugin_id)
    typed_version = _version(version)
    return _mutate_plugin(
        service=service,
        idempotency=idempotency,
        idempotency_key=idempotency_key,
        scope=f"plugin.enable:{admin.persisted_id}:{plugin_id}:{version}",
        payload={"plugin_id": plugin_id, "version": version, "operation": "enable"},
        action=lambda: service.request_enabled(typed_plugin_id, typed_version),
    )


@router.post("/{plugin_id}/{version}/disable", response_model=PluginVersionView)
def disable_plugin(
    plugin_id: str,
    version: str,
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PluginVersionView:
    typed_plugin_id = _plugin_id(plugin_id)
    typed_version = _version(version)
    return _mutate_plugin(
        service=service,
        idempotency=idempotency,
        idempotency_key=idempotency_key,
        scope=f"plugin.disable:{admin.persisted_id}:{plugin_id}:{version}",
        payload={"plugin_id": plugin_id, "version": version, "operation": "disable"},
        action=lambda: service.request_disabled(typed_plugin_id, typed_version),
    )


@router.post("/{plugin_id}/{version}/rollback", response_model=PluginVersionView)
def rollback_plugin(
    plugin_id: str,
    version: str,
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PluginVersionView:
    typed_plugin_id = _plugin_id(plugin_id)
    typed_version = _version(version)
    return _mutate_plugin(
        service=service,
        idempotency=idempotency,
        idempotency_key=idempotency_key,
        scope=f"plugin.rollback:{admin.persisted_id}:{plugin_id}:{version}",
        payload={"plugin_id": plugin_id, "version": version, "operation": "rollback"},
        action=lambda: service.rollback(typed_plugin_id, typed_version),
    )


@router.delete("/{plugin_id}/{version}", status_code=status.HTTP_204_NO_CONTENT)
def remove_plugin(
    plugin_id: str,
    version: str,
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    typed_plugin_id = _plugin_id(plugin_id)
    typed_version = _version(version)
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"plugin.remove:{admin.persisted_id}:{plugin_id}:{version}",
        payload={"plugin_id": plugin_id, "version": version, "operation": "remove"},
        response_model=None,
    )
    if result.replayed:
        return
    try:
        service.remove(typed_plugin_id, typed_version)
        complete_idempotency(
            idempotency,
            result.reservation,
            {},
            response_status=status.HTTP_204_NO_CONTENT,
        )
    except KeyError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise
