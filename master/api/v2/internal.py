"""V2 Agent 内部下载端点。"""

from __future__ import annotations

from urllib.parse import quote

from aetp_protocol.ids import BusinessId
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from master.api.v1.dependencies import ScriptStorageServiceDep, UowFactoryDep

router = APIRouter(prefix="/api/v2/internal", tags=["v2-internal"])


@router.get("/scripts/{script_id}/download")
def download_v2_script(
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
        raise HTTPException(status_code=422, detail="V2 ScriptDefinition ID 不合法") from exc
    download_service = request.app.state.container.v2_script_download_service()
    if not download_service.verify(script_id, expires, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="签名无效或已过期",
        )
    with uow_factory() as uow:
        record = uow.script_definitions.get(typed_script_id, 1)
    if record is None or record.definition.script_definition_id != typed_script_id:
        raise HTTPException(status_code=404, detail="V2 ScriptDefinition 不存在")
    source = record.definition.source
    file_ref = storage_service.script_key(
        source.script_id.root,
        source.version,
        source.filename,
    )
    if not storage_service.script_exists(file_ref):
        raise HTTPException(status_code=404, detail="V2 脚本文件缺失")
    return StreamingResponse(
        storage_service.open_script(file_ref),
        media_type="application/octet-stream",
        headers={
            "X-Checksum-Sha256": source.sha256.root,
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(source.filename, safe='')}",
        },
    )
