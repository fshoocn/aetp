"""内部端点（仅 Agent 服务身份，不走用户 JWT）。

P4.7 脚本签名下载：Agent 携带 ``run.assign`` 中签发的限时
``download_url`` 访问，校验 HMAC 签名与过期后返回脚本包，响应头附
``X-Checksum-Sha256`` 供 Agent 校验内容哈希（§7.4/§18.8）。

文件读写统一经 ``ScriptStorageService``（Storage 端口），不直接
访问文件系统，便于将来切换到 OSS/S3。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

from master.api.v1.dependencies import (
    ArtifactServiceDep,
    ScriptDownloadServiceDep,
    ScriptStorageServiceDep,
    UowFactoryDep,
)

router = APIRouter(prefix="/internal", tags=["v1-internal"])


@router.get("/scripts/{script_id}/download")
def download_script(
    script_id: str,
    expires: int,
    signature: str,
    uow_factory: UowFactoryDep,
    download_service: ScriptDownloadServiceDep,
    storage_service: ScriptStorageServiceDep,
) -> StreamingResponse:
    """按签名 URL 下载脚本包（Agent 下载后校验 sha256）。"""
    if not download_service.verify(script_id, expires, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="签名无效或已过期",
        )
    with uow_factory() as uow:
        script = uow.test_scripts.get_by_script_id(script_id)
    if script is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="脚本不存在",
        )
    if not storage_service.script_exists(script.file_ref):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="脚本文件缺失",
        )
    filename = f"{script.name}-v{script.version}"
    return StreamingResponse(
        storage_service.open_script(script.file_ref),
        media_type="application/octet-stream",
        headers={
            "X-Checksum-Sha256": script.sha256,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/runs/{run_id}/artifacts", status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    run_id: str,
    project_id: str,
    node_id: str,
    kind: str,
    file: UploadFile,
    artifact_service: ArtifactServiceDep,
    shard_id: str | None = None,
) -> dict:
    """Agent 上传结束产物（报告/日志归档/数据），写 run_artifacts。

    §7.4 内部端点：仅 Agent 服务身份调用（不走用户 JWT）。kind 由调用方
    声明（report/log_archive/data），数据内容经 Storage 端口落盘。
    """
    if kind not in ("report", "log_archive", "data"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"非法产物类型: {kind}",
        )
    data = await file.read()
    artifact = artifact_service.register_artifact(
        run_id=run_id,
        project_id=project_id,
        node_id=node_id,
        kind=kind,
        filename=file.filename or "artifact",
        data=data,
        shard_id=shard_id,
    )
    return {
        "artifact_id": artifact.artifact_id,
        "run_id": artifact.run_id,
        "kind": artifact.kind.value,
        "size": artifact.size,
        "sha256": artifact.sha256,
    }
