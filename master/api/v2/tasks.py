"""AETP V2 多脚本任务、Run Snapshot 和调度 API。"""

from __future__ import annotations

from aetp_protocol.execution import TriggerType as V2TriggerType
from aetp_protocol.ids import BusinessId, new_id
from aetp_protocol.task import RunSnapshot, ScriptDefinition
from aetp_protocol.task import TestTask as ProtocolTestTask
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from master.api.v1.dependencies import (
    V2SchedulerServiceDep,
    V2TaskServiceDep,
)
from master.api.v1.permissions import ProjectManagerDep, ProjectOperatorDep
from master.application.services.v2_scheduler_service import V2ScheduleResult
from master.application.services.v2_task_service import V2RunCreated
from master.domain.models import ScriptDefinitionRecord, V2TestTaskRecord

router = APIRouter(prefix="/api/v2/projects", tags=["v2-tasks"])


class ScriptDefinitionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: ScriptDefinition


class V2TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: ProtocolTestTask


class V2TaskView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: ProtocolTestTask
    created_by: int


class V2RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: BusinessId
    task_revision: int | None = Field(default=None, ge=1)
    run_id: BusinessId | None = None
    trigger_type: V2TriggerType = V2TriggerType.MANUAL_WEB
    original_run_id: BusinessId | None = None


class V2ShardView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    shard_id: BusinessId
    script_binding_id: BusinessId
    shard_index: int
    case_keys: tuple[str, ...]
    status: str


class V2RunView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: BusinessId
    task_id: BusinessId
    snapshot: RunSnapshot
    status: str
    shards: tuple[V2ShardView, ...]
    scheduled: int
    pending_shard_ids: tuple[BusinessId, ...]
    cancelled_shard_ids: tuple[BusinessId, ...]


def _definition_view(record: ScriptDefinitionRecord) -> ScriptDefinition:
    return record.definition


def _task_view(record: V2TestTaskRecord) -> V2TaskView:
    return V2TaskView(task=record.task, created_by=record.created_by)


def _run_view(created: V2RunCreated, schedule: V2ScheduleResult) -> V2RunView:
    return V2RunView(
        run_id=BusinessId(created.run.run_id),
        task_id=created.snapshot.task_id,
        snapshot=created.snapshot,
        status=created.run.status.value,
        shards=tuple(
            V2ShardView(
                shard_id=BusinessId(shard.shard_id),
                script_binding_id=BusinessId(shard.script_binding_id),
                shard_index=shard.shard_index,
                case_keys=tuple(shard.case_keys),
                status=shard.status.value,
            )
            for shard in created.shards
        ),
        scheduled=len(schedule.scheduled),
        pending_shard_ids=tuple(BusinessId(shard_id) for shard_id in schedule.pending_shard_ids),
        cancelled_shard_ids=tuple(BusinessId(shard_id) for shard_id in schedule.cancelled_shard_ids),
    )


@router.post(
    "/{project_id}/script-definitions",
    response_model=ScriptDefinition,
    status_code=status.HTTP_201_CREATED,
)
def create_script_definition(
    project_id: str,
    body: ScriptDefinitionCreateRequest,
    _access: ProjectManagerDep,
    service: V2TaskServiceDep,
) -> ScriptDefinition:
    try:
        typed_project_id = BusinessId(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="V2 项目 ID 不合法") from exc
    if body.definition.project_id != typed_project_id:
        raise HTTPException(status_code=422, detail="ScriptDefinition 项目与路径不一致")
    try:
        return _definition_view(service.register_script_definition(body.definition))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{project_id}/test-tasks",
    response_model=V2TaskView,
    status_code=status.HTTP_201_CREATED,
)
def create_v2_task(
    project_id: str,
    body: V2TaskCreateRequest,
    access: ProjectManagerDep,
    service: V2TaskServiceDep,
) -> V2TaskView:
    try:
        typed_project_id = BusinessId(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="V2 项目 ID 不合法") from exc
    if body.task.project_id != typed_project_id:
        raise HTTPException(status_code=422, detail="TestTask 项目与路径不一致")
    try:
        return _task_view(service.create_task(body.task, created_by=access.user.persisted_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{project_id}/runs",
    response_model=V2RunView,
    status_code=status.HTTP_201_CREATED,
)
def create_v2_run(
    project_id: str,
    body: V2RunCreateRequest,
    _access: ProjectOperatorDep,
    service: V2TaskServiceDep,
    scheduler: V2SchedulerServiceDep,
) -> V2RunView:
    try:
        typed_project_id = BusinessId(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="V2 项目 ID 不合法") from exc
    if body.trigger_type not in {V2TriggerType.MANUAL_WEB, V2TriggerType.API}:
        raise HTTPException(status_code=403, detail="retry/recovery Run 只能由 Master 内部服务创建")
    try:
        created = service.create_run(
            body.task_id,
            project_id=typed_project_id,
            task_revision=body.task_revision,
            trigger_type=body.trigger_type,
            run_id=body.run_id or BusinessId(new_id()),
            original_run_id=body.original_run_id,
        )
        schedule = scheduler.schedule_run(BusinessId(created.run.run_id))
        return _run_view(created, schedule)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["router"]
