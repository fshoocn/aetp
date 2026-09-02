"""AETP V2 多脚本任务、Run Snapshot 和调度 API。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aetp_protocol.execution import TriggerType as V2TriggerType
from aetp_protocol.ids import BusinessId, new_id
from aetp_protocol.task import RunSnapshot, ScriptDefinition
from aetp_protocol.task import TestTask as ProtocolTestTask
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from master.api.v1.dependencies import (
    ArtifactServiceDep,
    EventPublisherDep,
    UowFactoryDep,
    V2SchedulerServiceDep,
    V2TaskServiceDep,
)
from master.api.v1.permissions import ProjectAccessDep, ProjectManagerDep, ProjectOperatorDep
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


class V2RunListView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: BusinessId
    task_id: BusinessId
    task_revision: int
    status: str
    trigger_type: V2TriggerType
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class V2RunResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: BusinessId
    passed: bool
    status: str
    node_id: BusinessId | None
    metrics: dict[str, Any]
    data: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None


class V2RunCaseResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: BusinessId
    shard_id: BusinessId
    case_key: str
    attempt_no: int
    status: str
    duration_ms: int | None
    error_summary: str | None
    detail: dict[str, Any] | None


class V2RunArtifactView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: BusinessId
    run_id: BusinessId
    shard_id: BusinessId | None
    node_id: BusinessId | None
    kind: str
    filename: str
    file_ref: str
    content_type: str
    size: int
    sha256: str
    derived_from: BusinessId | None
    uploaded_at: datetime


class V2RunDetailView(V2RunView):
    result: V2RunResultView | None
    case_results: tuple[V2RunCaseResultView, ...]
    artifacts: tuple[V2RunArtifactView, ...]


class V2RunEventView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    sequence: int | None
    event_type: str
    aggregate_id: str
    payload: dict[str, Any]
    occurred_at: datetime | None


class V2RunLogView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    run_id: BusinessId
    node_id: str
    sequence: int
    level: str
    message: str
    detail: dict[str, Any] | None
    occurred_at: datetime | None


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


def _run_list_view(run) -> V2RunListView:
    if run.snapshot is None:
        raise ValueError("V2 Run 缺少不可变 Snapshot")
    return V2RunListView(
        run_id=BusinessId(run.run_id),
        task_id=BusinessId(run.task_id),
        task_revision=run.task_revision or run.snapshot.task_revision,
        status=run.status.value,
        trigger_type=V2TriggerType(run.trigger_type.value),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _run_detail_view(run, shards, result, case_results, artifacts) -> V2RunDetailView:
    if run.snapshot is None:
        raise ValueError("V2 Run 缺少不可变 Snapshot")
    return V2RunDetailView(
        run_id=BusinessId(run.run_id),
        task_id=BusinessId(run.task_id),
        snapshot=run.snapshot,
        status=run.status.value,
        shards=tuple(
            V2ShardView(
                shard_id=BusinessId(shard.shard_id),
                script_binding_id=BusinessId(shard.script_binding_id),
                shard_index=shard.shard_index,
                case_keys=tuple(shard.case_keys),
                status=shard.status.value,
            )
            for shard in shards
        ),
        scheduled=0,
        pending_shard_ids=tuple(
            BusinessId(shard.shard_id)
            for shard in shards
            if shard.status.value in {"pending", "dispatching", "waiting_recovery"}
        ),
        cancelled_shard_ids=tuple(
            BusinessId(shard.shard_id)
            for shard in shards
            if shard.status.value == "cancelled"
        ),
        result=(
            V2RunResultView(
                result_id=BusinessId(result.result_id),
                passed=result.passed,
                status=result.status.value,
                node_id=BusinessId(result.node_id) if result.node_id else None,
                metrics=dict(result.metrics or {}),
                data=dict(result.data or {}),
                started_at=result.started_at,
                finished_at=result.finished_at,
            )
            if result is not None
            else None
        ),
        case_results=tuple(
            V2RunCaseResultView(
                run_id=BusinessId(item.run_id),
                shard_id=BusinessId(item.shard_id),
                case_key=item.case_key,
                attempt_no=item.attempt_no,
                status=item.status.value,
                duration_ms=item.duration_ms,
                error_summary=item.error_summary,
                detail=item.detail,
            )
            for item in case_results
        ),
        artifacts=tuple(
            V2RunArtifactView(
                artifact_id=BusinessId(item.artifact_id),
                run_id=BusinessId(item.run_id),
                shard_id=BusinessId(item.shard_id) if item.shard_id else None,
                node_id=BusinessId(item.node_id) if item.node_id else None,
                kind=item.kind.value,
                filename=item.filename,
                file_ref=item.file_ref,
                content_type=item.content_type,
                size=item.size,
                sha256=item.sha256,
                derived_from=BusinessId(item.derived_from) if item.derived_from else None,
                uploaded_at=item.uploaded_at,
            )
            for item in artifacts
        ),
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


@router.get(
    "/{project_id}/script-definitions",
    response_model=list[ScriptDefinition],
)
def list_script_definitions(
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    enabled: bool | None = None,
) -> list[ScriptDefinition]:
    typed_project_id = _project_id(project_id)
    with uow_factory() as uow:
        records = uow.script_definitions.list_by_project(typed_project_id, enabled=enabled)
    return [record.definition for record in records]


@router.get(
    "/{project_id}/script-definitions/{script_definition_id}",
    response_model=ScriptDefinition,
)
def get_script_definition(
    project_id: str,
    script_definition_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    revision: int | None = None,
) -> ScriptDefinition:
    typed_project_id = _project_id(project_id)
    try:
        definition_id = BusinessId(script_definition_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="V2 ScriptDefinition ID 不合法") from exc
    if revision is None or revision < 1:
        raise HTTPException(status_code=422, detail="revision 必须大于 0")
    with uow_factory() as uow:
        record = uow.script_definitions.get(definition_id, revision)
    if record is None or record.definition.project_id != typed_project_id:
        raise HTTPException(status_code=404, detail="ScriptDefinition 不存在")
    return record.definition


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


@router.get(
    "/{project_id}/test-tasks",
    response_model=list[V2TaskView],
)
def list_v2_tasks(
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    enabled: bool | None = None,
) -> list[V2TaskView]:
    typed_project_id = _project_id(project_id)
    with uow_factory() as uow:
        records = uow.v2_test_tasks.list_by_project(typed_project_id, enabled=enabled)
    return [_task_view(record) for record in records]


@router.get(
    "/{project_id}/test-tasks/{task_id}",
    response_model=V2TaskView,
)
def get_v2_task(
    project_id: str,
    task_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    revision: int | None = None,
) -> V2TaskView:
    typed_project_id = _project_id(project_id)
    try:
        typed_task_id = BusinessId(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="V2 TestTask ID 不合法") from exc
    with uow_factory() as uow:
        record = uow.v2_test_tasks.get(typed_task_id, revision)
    if record is None or record.task.project_id != typed_project_id:
        raise HTTPException(status_code=404, detail="V2 TestTask 不存在")
    return _task_view(record)


@router.post(
    "/{project_id}/runs",
    response_model=V2RunView,
    status_code=status.HTTP_201_CREATED,
)
async def create_v2_run(
    project_id: str,
    body: V2RunCreateRequest,
    _access: ProjectOperatorDep,
    service: V2TaskServiceDep,
    scheduler: V2SchedulerServiceDep,
    event_publisher: EventPublisherDep,
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
        await event_publisher.publish(
            "run.created",
            {
                "run_id": created.run.run_id,
                "task_id": created.run.task_id,
                "project_id": typed_project_id.root,
            },
            project_id=typed_project_id.root,
            aggregate_id=created.run.run_id,
        )
        return _run_view(created, schedule)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/{project_id}/runs",
    response_model=list[V2RunListView],
)
def list_v2_runs(
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    limit: int = 100,
    offset: int = 0,
) -> list[V2RunListView]:
    typed_project_id = _project_id(project_id)
    if not 1 <= limit <= 1000 or offset < 0:
        raise HTTPException(status_code=422, detail="limit/offset 参数不合法")
    with uow_factory() as uow:
        runs = uow.task_runs.list(project_id=typed_project_id.root, limit=limit, offset=offset)
    return [_run_list_view(run) for run in runs if run.snapshot is not None]


@router.get(
    "/{project_id}/runs/{run_id}/logs",
    response_model=list[V2RunLogView],
)
def list_v2_run_logs(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    after_sequence: int = 0,
) -> list[V2RunLogView]:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, "V2 Run ID")
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence 不能小于 0")
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None:
            raise HTTPException(status_code=404, detail="V2 Run 不存在")
        logs = uow.run_logs.list_by_run(run.run_id, after_sequence=after_sequence)
    return [
        V2RunLogView(
            id=item.id or 0,
            run_id=BusinessId(item.run_id),
            node_id=item.node_id,
            sequence=item.sequence,
            level=item.level.value,
            message=item.message,
            detail=item.detail,
            occurred_at=item.occurred_at,
        )
        for item in logs
    ]


@router.get(
    "/{project_id}/runs/{run_id}/events",
    response_model=list[V2RunEventView],
)
def list_v2_run_events(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> list[V2RunEventView]:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, "V2 Run ID")
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None:
            raise HTTPException(status_code=404, detail="V2 Run 不存在")
        events = uow.domain_events.list_by_aggregate(run.run_id, project_id=typed_project_id.root)
    return [
        V2RunEventView(
            event_id=item.event_id,
            sequence=item.sequence,
            event_type=item.event_type,
            aggregate_id=item.aggregate_id,
            payload=item.payload,
            occurred_at=item.occurred_at,
        )
        for item in events
    ]


@router.get(
    "/{project_id}/runs/{run_id}/artifacts",
    response_model=list[V2RunArtifactView],
)
def list_v2_run_artifacts(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> list[V2RunArtifactView]:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, "V2 Run ID")
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None:
            raise HTTPException(status_code=404, detail="V2 Run 不存在")
        artifacts = uow.run_artifacts.list_by_run(run.run_id)
    return [
        V2RunArtifactView(
            artifact_id=BusinessId(item.artifact_id),
            run_id=BusinessId(item.run_id),
            shard_id=BusinessId(item.shard_id) if item.shard_id else None,
            node_id=BusinessId(item.node_id) if item.node_id else None,
            kind=item.kind.value,
            filename=item.filename,
            file_ref=item.file_ref,
            content_type=item.content_type,
            size=item.size,
            sha256=item.sha256,
            derived_from=BusinessId(item.derived_from) if item.derived_from else None,
            uploaded_at=item.uploaded_at,
        )
        for item in artifacts
    ]


@router.get("/{project_id}/runs/{run_id}/artifacts/{artifact_id}/download")
def download_v2_run_artifact(
    project_id: str,
    run_id: str,
    artifact_id: str,
    _access: ProjectAccessDep,
    artifact_service: ArtifactServiceDep,
) -> StreamingResponse:
    typed_run_id = _business_id(run_id, "V2 Run ID")
    _business_id(artifact_id, "Artifact ID")
    artifact = artifact_service.get_by_artifact_id(artifact_id, project_id)
    if artifact is None or artifact.run_id != typed_run_id.root:
        raise HTTPException(status_code=404, detail="Artifact 不存在")
    return StreamingResponse(
        artifact_service.open(artifact),
        media_type=artifact.content_type,
        headers={
            "X-Checksum-Sha256": artifact.sha256,
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
        },
    )


@router.get(
    "/{project_id}/runs/{run_id}",
    response_model=V2RunDetailView,
)
def get_v2_run(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> V2RunDetailView:
    typed_project_id = _project_id(project_id)
    try:
        typed_run_id = BusinessId(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="V2 Run ID 不合法") from exc
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None:
            raise HTTPException(status_code=404, detail="V2 Run 不存在")
        shards = uow.run_shards.list_by_run(run.run_id)
        result = uow.run_results.get_by_run_id(run.run_id)
        case_results = uow.run_case_results.list_by_run(run.run_id)
        artifacts = uow.run_artifacts.list_by_run(run.run_id)
    return _run_detail_view(run, shards, result, case_results, artifacts)


def _project_id(value: str) -> BusinessId:
    try:
        return BusinessId(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="V2 项目 ID 不合法") from exc


def _business_id(value: str, label: str) -> BusinessId:
    try:
        return BusinessId(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label} 不合法") from exc


__all__ = ["router"]
