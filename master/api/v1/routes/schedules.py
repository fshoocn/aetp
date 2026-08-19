"""项目范围任务调度计划 API（P8.2，D-18）。

定时/周期 Schedule CRUD，cron_expression 与 interval_seconds 互斥。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from master.api.v1.dependencies import ScheduleServiceDep
from master.api.v1.permissions import ProjectAccessDep, ProjectManagerDep
from master.api.v1.schemas import (
    ScheduleCreateRequest,
    ScheduleOut,
    ScheduleUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/tasks/{task_id}/schedules",
    tags=["v1-task-schedules"],
)


@router.get("", response_model=list[ScheduleOut])
def list_schedules(
    project_id: str,
    task_id: str,
    _access: ProjectAccessDep,
    service: ScheduleServiceDep,
) -> list[ScheduleOut]:
    """查询任务定义的调度计划。"""
    schedules = service.list_schedules(task_id)
    return [ScheduleOut.model_validate(s) for s in schedules]


@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
def create_schedule(
    project_id: str,
    task_id: str,
    body: ScheduleCreateRequest,
    _access: ProjectManagerDep,
    service: ScheduleServiceDep,
) -> ScheduleOut:
    """创建定时/周期调度计划（cron 与 interval 互斥）。"""
    try:
        schedule = service.create_schedule(
            project_id=project_id,
            task_id=task_id,
            cron_expression=body.cron_expression,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone,
            enabled=body.enabled,
            created_by=_access.user.persisted_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return ScheduleOut.model_validate(schedule)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
def update_schedule(
    project_id: str,
    task_id: str,
    schedule_id: str,
    body: ScheduleUpdateRequest,
    _access: ProjectManagerDep,
    service: ScheduleServiceDep,
) -> ScheduleOut:
    """更新调度计划。"""
    try:
        schedule = service.update_schedule(
            schedule_id,
            project_id=project_id,
            cron_expression=body.cron_expression,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return ScheduleOut.model_validate(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    project_id: str,
    task_id: str,
    schedule_id: str,
    _access: ProjectManagerDep,
    service: ScheduleServiceDep,
) -> None:
    """删除调度计划。"""
    try:
        service.delete_schedule(schedule_id, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
