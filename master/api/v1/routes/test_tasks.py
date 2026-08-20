"""项目范围任务定义 API（P7.4，§18.4）。

任务定义（``test_tasks``）是可复用的执行模板：引用脚本版本 + 默认勾选用例
集合 + 节点/分割/重试策略；定义与执行分离（§18.1），手动触发产生 Run。

注意：路径使用 ``/test-tasks`` 与旧版 ``/tasks``（TaskService 占位任务）
区分，避免语义冲突。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from master.api.v1.dependencies import (
    TestTaskServiceDep,
)
from master.api.v1.permissions import ProjectAccessDep, ProjectManagerDep
from master.api.v1.schemas import TestTaskCreateRequest, TestTaskOut, TestTaskUpdateRequest
from master.application.services.test_task_service import TestTaskService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/test-tasks",
    tags=["v1-project-test-tasks"],
)


def _task_out(task, service: TestTaskService) -> TestTaskOut:
    """把保存时的节点能力软校验结果带回 Web。"""
    output = TestTaskOut.model_validate(task)
    validation = service.validate_node_selection(
        task.project_id, task.node_ids, task.script_id
    )
    return output.model_copy(update={"validation_warning": validation.warning})


@router.get("", response_model=list[TestTaskOut])
def list_test_tasks(
    project_id: str,
    _access: ProjectAccessDep,
    service: TestTaskServiceDep,
    enabled: bool | None = None,
) -> list[TestTaskOut]:
    """列出项目任务定义。"""
    tasks = service.list_tasks(project_id, enabled=enabled)
    return [TestTaskOut.model_validate(t) for t in tasks]


@router.post("", response_model=TestTaskOut, status_code=status.HTTP_201_CREATED)
def create_test_task(
    project_id: str,
    body: TestTaskCreateRequest,
    access: ProjectManagerDep,
    service: TestTaskServiceDep,
) -> TestTaskOut:
    """创建任务定义（§18.4：脚本已解析、case 存在、节点 ⊆ 项目绑定）。"""
    try:
        task = service.create_task(
            project_id=project_id,
            name=body.name,
            script_id=body.script_id,
            default_case_selection=body.default_case_selection,
            node_ids=body.node_ids,
            split_policy=body.split_policy,
            retry_policy=body.retry_policy,
            timeout_s=body.timeout_s,
            priority=body.priority,
            created_by=access.user.persisted_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _task_out(task, service)


@router.get("/{task_id}", response_model=TestTaskOut)
def get_test_task(
    project_id: str,
    task_id: str,
    _access: ProjectAccessDep,
    service: TestTaskServiceDep,
) -> TestTaskOut:
    """查询任务定义详情（项目范围）。"""
    task = service.get_task(task_id, project_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务定义不存在")
    return _task_out(task, service)


@router.patch("/{task_id}", response_model=TestTaskOut)
def update_test_task(
    project_id: str,
    task_id: str,
    body: TestTaskUpdateRequest,
    access: ProjectManagerDep,
    service: TestTaskServiceDep,
) -> TestTaskOut:
    """更新任务定义（全量字段，缺失保持原值）。"""
    try:
        task = service.update_task(
            task_id,
            project_id=project_id,
            name=body.name,
            script_id=body.script_id,
            default_case_selection=body.default_case_selection,
            node_ids=body.node_ids,
            split_policy=body.split_policy,
            retry_policy=body.retry_policy,
            timeout_s=body.timeout_s,
            enabled=body.enabled,
            priority=body.priority,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return TestTaskOut.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_test_task(
    project_id: str,
    task_id: str,
    _access: ProjectManagerDep,
    service: TestTaskServiceDep,
) -> None:
    """删除任务定义（硬删除；历史 Run 引用置空，执行记录保留）。"""
    service.delete_task(task_id, project_id)
