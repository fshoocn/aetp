"""内部端点（仅 Agent 服务身份，不走用户 JWT）。

P4.7 脚本签名下载：Agent 携带 ``run.assign`` 中签发的限时
``download_url`` 访问，校验 HMAC 签名与过期后返回脚本包，响应头附
``X-Checksum-Sha256`` 供 Agent 校验内容哈希（§7.4/§18.8）。

P5.5 插件签名下载：Agent 检查本地插件版本缺失/不兼容时，携带 ``plugin_ref``
中的限时 ``download_url`` 下载同一插件包的 ZIP 分发，校验 sha256 后安装
（§18.8）。

文件读写统一经 ``Storage`` 端口/受控插件目录，不直接访问文件系统。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from aetp_protocol.ids import PluginId, SemVer
from aetp_protocol.plugin_types import PluginStatus
from fastapi import APIRouter, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

from master.api.v1.dependencies import (
    ArtifactServiceDep,
    ArtifactUploadSigningServiceDep,
    PluginDownloadServiceDep,
    PluginManagerDep,
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
            detail={"code": "AUTH_FORBIDDEN", "message": "签名无效或已过期", "data": None},
        )
    with uow_factory() as uow:
        script = uow.test_scripts.get_by_script_id(script_id)
    if script is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SCRIPT_NOT_FOUND", "message": "脚本不存在", "data": None},
        )
    if not storage_service.script_exists(script.file_ref):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SCRIPT_NOT_FOUND", "message": "脚本文件缺失", "data": None},
        )
    filename = f"{script.name}-v{script.version}"
    # RFC 5987 编码：文件名可能含中文，用 UTF-8 编码避免 latin-1 header 报错
    encoded = quote(filename, safe="")
    return StreamingResponse(
        storage_service.open_script(script.file_ref),
        media_type="application/octet-stream",
        headers={
            "X-Checksum-Sha256": script.sha256,
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        },
    )


@router.get("/plugins/{plugin_id}/download")
def download_plugin(
    plugin_id: str,
    expires: int,
    signature: str,
    plugin_manager: PluginManagerDep,
    download_service: PluginDownloadServiceDep,
) -> StreamingResponse:
    """按签名 URL 下载已安装插件包的 ZIP 分发（Agent 校验 sha256 后安装）。

    §18.8：Agent 收到 ``run.assign`` 时检查本地插件版本，缺失或版本不兼容
    时经本端点下载同一插件包，校验 SHA-256 后加载执行。
    """
    if not download_service.verify(plugin_id, expires, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="签名无效或已过期",
        )
    record = None
    for item in plugin_manager.list():
        if item.plugin_id == plugin_id:
            record = item
            break
    if record is None or not record.installed or not record.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="插件不存在或未安装",
        )
    archive = plugin_manager.archives / f"{record.sha256}.zip"
    if not archive.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="插件包文件缺失",
        )
    return StreamingResponse(
        open(archive, "rb"),
        media_type="application/zip",
        headers={
            "X-Checksum-Sha256": record.sha256,
            "Content-Disposition": (f"attachment; filename*=UTF-8''{quote(record.filename, safe='')}"),
        },
    )


@router.get("/plugins/{plugin_id}/{version}/download")
def download_v2_plugin(
    plugin_id: str,
    version: str,
    expires: int,
    signature: str,
    uow_factory: UowFactoryDep,
    download_service: PluginDownloadServiceDep,
) -> StreamingResponse:
    """按 V2 插件 ID、版本和签名下载 Master Registry 中的精确归档。"""
    try:
        typed_plugin_id = PluginId(plugin_id)
        typed_version = SemVer(version)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="插件 ID 或版本不合法") from exc
    if not download_service.verify_version(typed_plugin_id, typed_version, expires, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="签名无效或已过期",
        )
    with uow_factory() as uow:
        record = uow.plugin_versions.get(typed_plugin_id, typed_version)
    if record is None or record.status is PluginStatus.REMOVED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="V2 插件版本不存在或已移除",
        )
    archive = Path(record.archive_path).resolve()
    if not archive.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="V2 插件归档文件缺失",
        )
    return StreamingResponse(
        archive.open("rb"),
        media_type="application/zip",
        headers={
            "X-Checksum-Sha256": record.archive_sha256.root,
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(record.filename, safe='')}",
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
    signing_service: ArtifactUploadSigningServiceDep,
    expires: int,
    signature: str,
    shard_id: str | None = None,
    attempt_id: str | None = None,
) -> dict:
    """Agent 上传结束产物（报告/日志归档/数据），写 run_artifacts。

    §7.4 内部端点：仅 Agent 服务身份调用（不走用户 JWT）。kind 由调用方
    声明（report/log_archive/data），数据内容经 Storage 端口落盘。
    """
    if not signing_service.verify(
        run_id,
        project_id,
        node_id,
        shard_id or "",
        attempt_id,
        expires,
        signature,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="签名无效或已过期",
        )
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
        attempt_id=attempt_id,
        content_type=file.content_type or "application/octet-stream",
    )
    return {
        "artifact_id": artifact.artifact_id,
        "run_id": artifact.run_id,
        "kind": artifact.kind.value,
        "size": artifact.size,
        "sha256": artifact.sha256,
        "project_id": project_id,
        "shard_id": artifact.shard_id,
        "attempt_id": artifact.attempt_id,
        "filename": artifact.filename,
        "content_type": artifact.content_type,
        "derived_from": artifact.derived_from,
    }
