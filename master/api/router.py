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
from fastapi.responses import FileResponse
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


@router.get("/{plugin_id}/{version}/download")
def download_plugin_archive(
    plugin_id: str,
    version: str,
    request: Request,
    _admin: PlatformAdminDep,
) -> FileResponse:
    """下载插件归档（平台管理员）。

    已移除/停用/历史版本都可下载：归档文件在治理里不可变保留（逻辑移除不清文件），
    用于备份或"无新版本时仍可获取原包 / 有新版本时下载历史版本"。
    """
    from pathlib import Path

    try:
        typed_plugin_id = PluginId(plugin_id)
        typed_version = SemVer(version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="插件 ID/版本不合法") from exc
    container = request.app.state.container
    with container.uow_factory()() as uow:
        record = uow.plugin_versions.get(typed_plugin_id, typed_version)
    if record is None:
        raise HTTPException(status_code=404, detail="插件版本不存在")
    archive_path = Path(record.archive_path)
    if not archive_path.is_file():
        raise HTTPException(status_code=404, detail="插件归档文件缺失")
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=record.filename or f"{plugin_id}-{version}.zip",
    )


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
    request: Request,
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PluginVersionView:
    typed_plugin_id = _plugin_id(plugin_id)
    typed_version = _version(version)
    response = _mutate_plugin(
        service=service,
        idempotency=idempotency,
        idempotency_key=idempotency_key,
        scope=f"plugin.enable:{admin.persisted_id}:{plugin_id}:{version}",
        payload={"plugin_id": plugin_id, "version": version, "operation": "enable"},
        action=lambda: service.enable(typed_plugin_id, typed_version),
    )
    _refresh_hot_plugins(request)
    return response


@router.post("/{plugin_id}/{version}/disable", response_model=PluginVersionView)
def disable_plugin(
    plugin_id: str,
    version: str,
    request: Request,
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PluginVersionView:
    typed_plugin_id = _plugin_id(plugin_id)
    typed_version = _version(version)
    response = _mutate_plugin(
        service=service,
        idempotency=idempotency,
        idempotency_key=idempotency_key,
        scope=f"plugin.disable:{admin.persisted_id}:{plugin_id}:{version}",
        payload={"plugin_id": plugin_id, "version": version, "operation": "disable"},
        action=lambda: service.disable(typed_plugin_id, typed_version),
    )
    _refresh_hot_plugins(request)
    return response


@router.post("/{plugin_id}/{version}/rollback", response_model=PluginVersionView)
def rollback_plugin(
    plugin_id: str,
    version: str,
    request: Request,
    admin: PlatformAdminDep,
    service: Annotated[PluginGovernanceService, Depends(get_plugin_service)],
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PluginVersionView:
    typed_plugin_id = _plugin_id(plugin_id)
    typed_version = _version(version)
    response = _mutate_plugin(
        service=service,
        idempotency=idempotency,
        idempotency_key=idempotency_key,
        scope=f"plugin.rollback:{admin.persisted_id}:{plugin_id}:{version}",
        payload={"plugin_id": plugin_id, "version": version, "operation": "rollback"},
        action=lambda: service.rollback(typed_plugin_id, typed_version),
    )
    _refresh_hot_plugins(request)
    return response


def _refresh_hot_plugins(request: Request) -> None:
    """状态变更后即时把 DB 的 ENABLED 插件集重投影到进程内装配面（热插拔，无需重启）。"""
    container = getattr(request.app.state, "container", None)
    if container is None:
        return
    try:
        container.plugin_hot_reload().refresh()
    except Exception:  # noqa: BLE001 - 热重载失败不应让已成功的状态变更报错
        import logging

        logging.getLogger(__name__).exception("插件热重载失败")


def _uninstall_from_agents(
    request: Request,
    plugin_id: PluginId,
    version: SemVer,
    actor_id: int | None,
) -> None:
    """治理移除插件版本后，把它从所有装有它的 Agent 上卸载（尽力而为）。

    - 所有节点上指向该版本的期望会被清除（防止随后对账复装）；
    - 在线节点直接下发 REMOVE 同步；离线节点跳过并记录日志，
      其上的插件版本只能等待 Agent 端手工清理（期望已清除，不会被复装）。
    """
    container = getattr(request.app.state, "container", None)
    if container is None:
        return
    try:
        container.node_plugin_reconciler().uninstall_plugin_everywhere(
            plugin_id,
            version,
            actor_id=actor_id,
        )
    except Exception:  # noqa: BLE001 - Agent 清理失败不应让已成功的移除报错
        import logging

        logging.getLogger(__name__).exception(
            "插件移除后的 Agent 卸载下发失败: %s@%s", plugin_id.root, version.root
        )


@router.delete("/{plugin_id}/{version}", status_code=status.HTTP_204_NO_CONTENT)
def remove_plugin(
    plugin_id: str,
    version: str,
    request: Request,
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
        _refresh_hot_plugins(request)
        _uninstall_from_agents(request, typed_plugin_id, typed_version, admin.persisted_id)
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
