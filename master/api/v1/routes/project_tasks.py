"""项目范围任务 API。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, status

from master.api.v1.dependencies import EventPublisherDep, TaskServiceDep
from master.api.v1.permissions import ProjectAccessDep, ProjectOperatorDep
from master.api.v1.schemas import TaskCreate, TaskLogOut, TaskOut

router = APIRouter(
    prefix="/projects/{project_id}/tasks",
    tags=["v1-project-tasks"],
)


@router.get("", response_model=list[TaskOut])
def list_project_tasks(
    project_id: str,
    _access: ProjectAccessDep,
    service: TaskServiceDep,
    device_id: str | None = None,
    status_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[TaskOut]:
    """查询项目任务（分页）；仅返回当前 project_id 的任务。"""
    tasks = service.list_all(
        project_id=project_id,
        device_id=device_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return [TaskOut.model_validate(task) for task in tasks]


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_project_task(
    project_id: str,
    body: TaskCreate,
    access: ProjectOperatorDep,
    service: TaskServiceDep,
    event_publisher: EventPublisherDep,
) -> TaskOut:
    """创建项目任务；目标设备必须属于项目启用节点。

    创建成功后通过 SSE 广播 task.created 事件。
    """
    task = service.create(
        project_id=project_id,
        device_id=body.device_id,
        command=body.command,
        created_by=access.user.persisted_id,
    )
    out = TaskOut.model_validate(task)
    await event_publisher.publish(
        "task.created",
        json.loads(out.model_dump_json()),
        project_id=project_id,
        aggregate_id=task.task_id,
    )
    return out


@router.get("/{task_id}", response_model=TaskOut)
def get_project_task(
    project_id: str,
    task_id: str,
    _access: ProjectAccessDep,
    service: TaskServiceDep,
) -> TaskOut:
    """查询项目任务详情；跨项目 task_id 统一返回 404。"""
    task = service.get_by_id(task_id, project_id=project_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return TaskOut.model_validate(task)


@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
def get_project_task_logs(
    project_id: str,
    task_id: str,
    _access: ProjectAccessDep,
    service: TaskServiceDep,
) -> list[TaskLogOut]:
    """查询项目任务日志；跨项目 task_id 不可读取。"""
    logs = service.get_logs(task_id, project_id=project_id)
    return [TaskLogOut.model_validate(log) for log in logs]
