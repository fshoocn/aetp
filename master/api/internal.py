""" Agent 内部下载端点。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from aetp_protocol.ids import BusinessId, PluginId, SemVer
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse

from master.api.dependencies import ScriptStorageServiceDep, UowFactoryDep

router = APIRouter(prefix="/api/v2/internal", tags=["internal"])


@router.get("/plugins/{plugin_id}/{version}/download")
def download_plugin(
    plugin_id: str,
    version: str,
    request: Request,
    expires: int,
    signature: str,
    uow_factory: UowFactoryDep,
) -> FileResponse:
    try:
        typed_plugin_id = PluginId(plugin_id)
        typed_version = SemVer(version)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="插件 ID 或版本不合法") from exc

    download_service = request.app.state.container.plugin_download_service()
    if not download_service.verify_version(typed_plugin_id, typed_version, expires, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="签名无效或已过期",
        )
    with uow_factory() as uow:
        record = uow.plugin_versions.get(typed_plugin_id, typed_version)
    if record is None:
        raise HTTPException(status_code=404, detail="插件版本不存在")
    archive_path = Path(record.archive_path)
    if not archive_path.is_file():
        raise HTTPException(status_code=404, detail="插件归档文件缺失")
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=record.filename,
        headers={"X-Checksum-Sha256": record.archive_sha256.root},
    )


@router.get("/scripts/{script_id}/download")
def download_script(
    script_id: str,
    request: Request,
    expires: int,
    signature: str,
    uow_factory: UowFactoryDep,
    storage_service: ScriptStorageServiceDep,
) -> StreamingResponse:
    try:
        typed_script_id = BusinessId(script_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" ScriptDefinition ID 不合法") from exc
    download_service = request.app.state.container.script_download_service()
    if not download_service.verify(script_id, expires, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="签名无效或已过期",
        )
    with uow_factory() as uow:
        record = uow.script_definitions.get(typed_script_id, 1)
    if record is None or record.definition.script_definition_id != typed_script_id:
        raise HTTPException(status_code=404, detail=" ScriptDefinition 不存在")
    source = record.definition.source
    file_ref = storage_service.script_key(
        source.script_id.root,
        source.version,
        source.filename,
    )
    if not storage_service.script_exists(file_ref):
        raise HTTPException(status_code=404, detail=" 脚本文件缺失")
    return StreamingResponse(
        storage_service.open_script(file_ref),
        media_type="application/octet-stream",
        headers={
            "X-Checksum-Sha256": source.sha256.root,
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(source.filename, safe='')}",
        },
    )
