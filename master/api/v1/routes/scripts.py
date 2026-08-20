"""项目范围脚本库 API（P7.3，§18.3）。

上传测试脚本包（multipart：file + task_type + name + config JSON）→ Master 插件
验证 + 解析 → 写 ``test_scripts`` / ``script_cases``；查询脚本列表/详情/用例索引；
用户侧下载脚本包（Agent 走内部签名端点 P4.7）。
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

from master.api.v1.dependencies import (
    ScriptServiceDep,
    ScriptStorageServiceDep,
    ScriptVerificationServiceDep,
    UowFactoryDep,
)
from master.api.v1.permissions import ProjectAccessDep, ProjectManagerDep
from master.api.v1.schemas import (
    ScriptCaseOut,
    ScriptOut,
    ScriptVerifyDispatchOut,
    ScriptVerifyRequest,
    ScriptVerifyResultOut,
)
from master.application.errors import ScriptNotFoundError
from master.application.services.script_service import ScriptDeleteError, ScriptUploadError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/scripts",
    tags=["v1-project-scripts"],
)


@router.post("", response_model=ScriptOut, status_code=status.HTTP_201_CREATED)
async def upload_script(
    project_id: str,
    access: ProjectManagerDep,
    script_service: ScriptServiceDep,
    task_type: str = Form(min_length=1, max_length=64),
    name: str = Form(min_length=1, max_length=128),
    config: str = Form(default="{}", max_length=65536),
    file: UploadFile = File(...),
) -> ScriptOut:
    """上传测试脚本包并触发验证 + 解析（§18.3 步骤 1~3）。

    需要项目 maintainer/owner 或平台管理员。上传内容经插件
    ``verify_script`` 快速失败后由 Master 面 ``parse_cases`` 生成用例索引，
    同 hash 重复上传幂等复用。
    """
    try:
        config_dict = json.loads(config) if config else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"config 必须是合法 JSON: {exc}",
        ) from exc

    file_data = await file.read()
    if not file_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="上传文件为空",
        )

    try:
        script = await script_service.upload_script(
            project_id=project_id,
            task_type=task_type,
            name=name,
            config=config_dict,
            file_data=file_data,
            filename=file.filename or "script",
            created_by=access.user.persisted_id,
        )
    except ScriptUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 插件/仓储错误统一 422
        logger.exception("脚本上传失败: project=%s name=%s", project_id, name)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"脚本上传失败: {exc}",
        ) from exc
    return ScriptOut.model_validate(script)


@router.get("", response_model=list[ScriptOut])
def list_scripts(
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    limit: int = 100,
    offset: int = 0,
) -> list[ScriptOut]:
    """列出项目脚本版本（最新在前）。"""
    with uow_factory() as uow:
        scripts = uow.test_scripts.list_by_project(
            project_id, limit=limit, offset=offset
        )
    return [ScriptOut.model_validate(s) for s in scripts]


@router.get("/{script_id}", response_model=ScriptOut)
def get_script(
    project_id: str,
    script_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> ScriptOut:
    """查询脚本详情（项目范围）。"""
    with uow_factory() as uow:
        script = uow.test_scripts.get_by_script_id(script_id)
        if script is None or script.project_id != project_id:
            raise HTTPException(status_code=404, detail="脚本不存在")
    return ScriptOut.model_validate(script)


@router.get("/{script_id}/cases", response_model=list[ScriptCaseOut])
def list_script_cases(
    project_id: str,
    script_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> list[ScriptCaseOut]:
    """查询脚本用例索引（case 勾选数据源，§18.4）。"""
    with uow_factory() as uow:
        script = uow.test_scripts.get_by_script_id(script_id)
        if script is None or script.project_id != project_id:
            raise HTTPException(status_code=404, detail="脚本不存在")
        cases = uow.script_cases.list_by_script(script_id)
    return [ScriptCaseOut.model_validate(c) for c in cases]


@router.post("/{script_id}/parse", response_model=ScriptOut)
async def reparse_script(
    project_id: str,
    script_id: str,
    access: ProjectManagerDep,
    script_service: ScriptServiceDep,
) -> ScriptOut:
    """重新执行脚本验证 + 用例解析（插件升级后显式触发，§18.3 步骤 6）。"""
    try:
        script = await script_service.reparse_script(script_id, project_id=project_id)
    except ScriptUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("脚本重解析失败: script_id=%s", script_id)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"脚本重解析失败: {exc}",
        ) from exc
    return ScriptOut.model_validate(script)


@router.delete("/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_script(
    project_id: str,
    script_id: str,
    _access: ProjectManagerDep,
    script_service: ScriptServiceDep,
) -> None:
    """删除项目脚本；仍被任务定义引用时返回 409。"""
    try:
        script_service.delete_script(script_id, project_id=project_id)
    except ScriptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ScriptDeleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{script_id}/verify", response_model=ScriptVerifyDispatchOut)
def verify_script_on_agent(
    project_id: str,
    script_id: str,
    body: ScriptVerifyRequest,
    _access: ProjectManagerDep,
    verification: ScriptVerificationServiceDep,
) -> ScriptVerifyDispatchOut:
    """向项目节点下发插件声明的 Agent 侧脚本验证。"""
    try:
        result = verification.request(
            project_id=project_id,
            script_id=script_id,
            node_id=body.node_id,
            config=body.config,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return ScriptVerifyDispatchOut.model_validate(result)


@router.get("/{script_id}/verify/{verify_id}", response_model=ScriptVerifyResultOut)
def get_verify_result(
    project_id: str,
    script_id: str,
    verify_id: str,
    _access: ProjectAccessDep,
    verification: ScriptVerificationServiceDep,
) -> ScriptVerifyResultOut:
    """查询 Agent 验证结果；结果由 script.verify-result MQTT 事件写入。"""
    result = verification.get_result(project_id, verify_id)
    if result is None or result.get("script_id") != script_id:
        raise HTTPException(status_code=404, detail="验证结果不存在")
    return ScriptVerifyResultOut.model_validate(result)


@router.get("/{script_id}/download")
def download_script(
    project_id: str,
    script_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    storage_service: ScriptStorageServiceDep,
) -> StreamingResponse:
    """下载脚本包（用户侧；Agent 走内部签名端点 P4.7）。"""
    with uow_factory() as uow:
        script = uow.test_scripts.get_by_script_id(script_id)
        if script is None or script.project_id != project_id:
            raise HTTPException(status_code=404, detail="脚本不存在")
    if not storage_service.script_exists(script.file_ref):
        raise HTTPException(status_code=404, detail="脚本文件缺失")
    filename = f"{script.name}-v{script.version}"
    return StreamingResponse(
        storage_service.open_script(script.file_ref),
        media_type="application/octet-stream",
        headers={
            "X-Checksum-Sha256": script.sha256,
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
            ),
        },
    )
