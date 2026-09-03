""" TestTask 调度计划 API。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from master.api.dependencies import IdempotencyServiceDep, ScheduleServiceDep
from master.api.permissions import ProjectAccessDep, ProjectManagerDep
from master.domain.models.task_schedule import TaskSchedule

from .idempotency import complete as complete_idempotency
from .idempotency import release as release_idempotency
from .idempotency import reserve_or_replay

router = APIRouter(
    prefix="/api/v2/projects/{project_id}/test-tasks/{task_id}/schedules",
    tags=["task-schedules"],
)


class ScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cron_expression: str | None = Field(default=None, max_length=128)
    interval_seconds: StrictInt | None = Field(default=None, ge=1, le=365 * 24 * 3600)
    timezone: str = Field(default="UTC", max_length=64)
    enabled: bool = True


class ScheduleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cron_expression: str | None = Field(default=None, max_length=128)
    interval_seconds: StrictInt | None = Field(default=None, ge=1, le=365 * 24 * 3600)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class ScheduleView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schedule_id: str
    task_id: str
    project_id: str
    cron_expression: str | None
    interval_seconds: int | None
    timezone: str
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


def _view(schedule: TaskSchedule) -> ScheduleView:
    return ScheduleView(
        schedule_id=schedule.schedule_id,
        task_id=schedule.task_id,
        project_id=schedule.project_id,
        cron_expression=schedule.cron_expression,
        interval_seconds=schedule.interval_seconds,
        timezone=schedule.timezone,
        enabled=schedule.enabled,
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


@router.get("", response_model=list[ScheduleView])
def list_schedules(
    project_id: str,
    task_id: str,
    _access: ProjectAccessDep,
    service: ScheduleServiceDep,
) -> list[ScheduleView]:
    return [_view(item) for item in service.list_schedules(task_id, project_id=project_id)]


@router.post("", response_model=ScheduleView, status_code=status.HTTP_201_CREATED)
def create_schedule(
    project_id: str,
    task_id: str,
    body: ScheduleCreateRequest,
    access: ProjectManagerDep,
    service: ScheduleServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScheduleView:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"schedule.create:{project_id}:{task_id}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ScheduleView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        schedule = service.create_schedule(
            project_id=project_id,
            task_id=task_id,
            cron_expression=body.cron_expression,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone,
            enabled=body.enabled,
            created_by=access.user.persisted_id,
        )
        response = _view(schedule)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_201_CREATED)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.patch("/{schedule_id}", response_model=ScheduleView)
def update_schedule(
    project_id: str,
    task_id: str,
    schedule_id: str,
    body: ScheduleUpdateRequest,
    access: ProjectManagerDep,
    service: ScheduleServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScheduleView:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"schedule.update:{project_id}:{task_id}:{schedule_id}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ScheduleView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        schedule = service.update_schedule(
            schedule_id,
            project_id=project_id,
            task_id=task_id,
            cron_expression=body.cron_expression,
            interval_seconds=body.interval_seconds,
            timezone=body.timezone,
            enabled=body.enabled,
        )
        response = _view(schedule)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_200_OK)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    project_id: str,
    task_id: str,
    schedule_id: str,
    access: ProjectManagerDep,
    service: ScheduleServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"schedule.delete:{project_id}:{task_id}:{schedule_id}:{access.user.persisted_id}",
        payload={"schedule_id": schedule_id, "operation": "delete"},
        response_model=None,
    )
    if result.replayed:
        return
    try:
        service.delete_schedule(schedule_id, project_id, task_id)
        complete_idempotency(idempotency, result.reservation, {}, response_status=status.HTTP_204_NO_CONTENT)
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise
