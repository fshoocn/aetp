""" Agent 内部下载端点。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from aetp_protocol.ids import BusinessId, PluginId, SemVer
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from master.api.dependencies import ScriptStorageServiceDep, UowFactoryDep

router = APIRouter(prefix="/api/v2/internal", tags=["internal"])


@router.post("/runs/{run_id}/artifacts")
async def upload_run_artifact(
    run_id: str,
    request: Request,
    project_id: str,
    node_id: str,
    shard_id: str,
    expires: int,
    signature: str,
    attempt_id: str | None = None,
    kind: str = "report",
    file: UploadFile = File(...),  # noqa: B008 - FastAPI 文件参数
) -> dict[str, object]:
    """接收 Agent 上传的 Run 产物（签名 multipart）。

    校验限时 HMAC 上传 URL（范围=run/project/node/shard/attempt）后，把文件交给
    ArtifactService.register_artifact 写文件 + 登记引用。返回
    ``{artifact_id, run_id, kind, filename, size, sha256}``。
    """
    container = request.app.state.container
    signing = container.artifact_upload_signing_service()
    if not signing.verify(
        run_id,
        project_id,
        node_id,
        shard_id,
        attempt_id,
        expires,
        signature,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Artifact 上传签名无效或已过期",
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="产物文件为空")
    try:
        artifact = container.artifact_service().register_artifact(
            run_id=run_id,
            project_id=project_id,
            node_id=node_id,
            kind=kind,
            filename=file.filename or "artifact",
            data=data,
            shard_id=shard_id,
            attempt_id=attempt_id,
            content_type=file.content_type or "application/octet-stream",
        )
    except ValueError as exc:
        # ARTIFACT_UPLOAD_CONFLICT / attempt 不一致等业务冲突 → 409
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "artifact_id": artifact.artifact_id,
        "run_id": artifact.run_id,
        "kind": artifact.kind.value,
        "filename": artifact.filename,
        "size": artifact.size,
        "sha256": artifact.sha256,
    }


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
