"""AETP  多脚本任务、Run Snapshot 和调度 API。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from aetp_protocol.artifacts import Configuration, TestCase
from aetp_protocol.execution import TriggerType
from aetp_protocol.ids import BusinessId, PluginId, SemVer, Sha256, new_id
from aetp_protocol.task import RunSnapshot, ScriptDefinition
from aetp_protocol.task import TestTask as ProtocolTestTask
from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from master.api.dependencies import (
    ArtifactServiceDep,
    EventPublisherDep,
    ExecutionServiceDep,
    IdempotencyServiceDep,
    SchedulerServiceDep,
    ScriptDefinitionServiceDep,
    TaskServiceDep,
    UowFactoryDep,
)
from master.api.permissions import ProjectAccessDep, ProjectManagerDep, ProjectOperatorDep
from master.application.services.scheduler_service import ScheduleResult
from master.application.services.script_definition_service import ScriptDefinitionError
from master.application.services.task_service import RunCreated
from master.domain.models import ScriptDefinitionRecord, TestTaskRecord

from .idempotency import complete as complete_idempotency
from .idempotency import release as release_idempotency
from .idempotency import reserve_or_replay as reserve_idempotency

router = APIRouter(prefix="/api/v2/projects", tags=["tasks"])


class ScriptDefinitionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: ScriptDefinition


class TaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: ProtocolTestTask


class TaskView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: ProtocolTestTask
    created_by: int


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: BusinessId
    task_revision: int | None = Field(default=None, ge=1)
    run_id: BusinessId | None = None
    trigger_type: TriggerType = TriggerType.MANUAL_WEB
    original_run_id: BusinessId | None = None
    case_filter: tuple[str, ...] | None = None


class RunActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(default="", max_length=1024)


class ShardView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    shard_id: BusinessId
    script_binding_id: BusinessId
    shard_index: int
    case_keys: tuple[str, ...]
    status: str


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: BusinessId
    task_id: BusinessId
    snapshot: RunSnapshot
    status: str
    trigger_type: TriggerType
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    shards: tuple[ShardView, ...]
    scheduled: int
    pending_shard_ids: tuple[BusinessId, ...]
    cancelled_shard_ids: tuple[BusinessId, ...]


class RunListView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: BusinessId
    task_id: BusinessId
    task_revision: int
    status: str
    trigger_type: TriggerType
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: BusinessId
    passed: bool
    status: str
    node_id: BusinessId | None
    metrics: dict[str, Any]
    data: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None


class RunCaseResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: BusinessId
    shard_id: BusinessId
    case_key: str
    attempt_no: int
    status: str
    duration_ms: int | None
    error_summary: str | None
    detail: dict[str, Any] | None


class RunArtifactView(BaseModel):
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


class RunDetailView(RunView):
    result: RunResultView | None
    case_results: tuple[RunCaseResultView, ...]
    artifacts: tuple[RunArtifactView, ...]


class RunEventView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    sequence: int | None
    event_type: str
    aggregate_id: str
    payload: dict[str, Any]
    occurred_at: datetime | None


class RunLogView(BaseModel):
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


def _task_view(record: TestTaskRecord) -> TaskView:
    return TaskView(task=record.task, created_by=record.created_by)


def _run_view(created: RunCreated, schedule: ScheduleResult) -> RunView:
    return RunView(
        run_id=BusinessId(created.run.run_id),
        task_id=created.snapshot.task_id,
        snapshot=created.snapshot,
        status=created.run.status.value,
        trigger_type=created.run.trigger_type,
        created_at=created.run.created_at,
        started_at=created.run.started_at,
        finished_at=created.run.finished_at,
        shards=tuple(
            ShardView(
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


def _run_list_view(run) -> RunListView:
    if run.snapshot is None:
        raise ValueError(" Run 缺少不可变 Snapshot")
    return RunListView(
        run_id=BusinessId(run.run_id),
        task_id=BusinessId(run.task_id),
        task_revision=run.task_revision or run.snapshot.task_revision,
        status=run.status.value,
        trigger_type=run.trigger_type,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _run_detail_view(run, shards, result, case_results, artifacts) -> RunDetailView:
    if run.snapshot is None:
        raise ValueError(" Run 缺少不可变 Snapshot")
    return RunDetailView(
        run_id=BusinessId(run.run_id),
        task_id=BusinessId(run.task_id),
        snapshot=run.snapshot,
        status=run.status.value,
        trigger_type=run.trigger_type,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        shards=tuple(
            ShardView(
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
            RunResultView(
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
            RunCaseResultView(
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
            RunArtifactView(
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
    access: ProjectManagerDep,
    service: TaskServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScriptDefinition:
    try:
        typed_project_id = BusinessId(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" 项目 ID 不合法") from exc
    if body.definition.project_id != typed_project_id:
        raise HTTPException(status_code=422, detail="ScriptDefinition 项目与路径不一致")
    result = reserve_idempotency(
        idempotency,
        idempotency_key,
        scope=f"script-definition.create:{typed_project_id.root}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=ScriptDefinition,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        response = _definition_view(service.register_script_definition(body.definition))
        complete_idempotency(
            idempotency,
            result.reservation,
            response,
            response_status=status.HTTP_201_CREATED,
        )
        return response
    except KeyError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.post(
    "/{project_id}/script-definitions/upload",
    response_model=ScriptDefinition,
    status_code=status.HTTP_201_CREATED,
)
async def upload_script_definition(
    project_id: str,
    access: ProjectManagerDep,
    service: ScriptDefinitionServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    name: str = Form(...),
    executor_plugin_id: str = Form(...),
    executor_version: str = Form(...),
    configuration: str = Form("{}"),
    cases: str = Form(""),  # 可选：插件 UI/后端已生成的用例（JSON TestCase[]）
    file: UploadFile = File(...),  # noqa: B008 - FastAPI 文件参数
) -> ScriptDefinition:
    typed_project_id = _project_id(project_id)
    try:
        plugin_id = PluginId(executor_plugin_id)
        version = SemVer(executor_version)
        raw_configuration = json.loads(configuration)
        if not isinstance(raw_configuration, dict):
            raise ValueError("configuration 必须是 JSON 对象")
        if {"schema_version", "schema_hash", "values"} <= raw_configuration.keys():
            typed_configuration = Configuration.model_validate(raw_configuration)
        else:
            values_json = json.dumps(
                raw_configuration,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            typed_configuration = Configuration(
                schema_version=1,
                schema_hash=Sha256(hashlib.sha256(values_json).hexdigest()),
                values=raw_configuration,
            )
        typed_cases: tuple[TestCase, ...] | None = None
        if cases.strip():
            raw_cases = json.loads(cases)
            if not isinstance(raw_cases, list):
                raise ValueError("cases 必须是 JSON 数组")
            typed_cases = tuple(TestCase.model_validate(item) for item in raw_cases)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f" ScriptDefinition 参数无效: {exc}") from exc
    file_data = await file.read()
    result = reserve_idempotency(
        idempotency,
        idempotency_key,
        scope=f"script-definition.upload:{typed_project_id.root}:{access.user.persisted_id}",
        payload={
            "name": name,
            "executor_plugin_id": executor_plugin_id,
            "executor_version": executor_version,
            "configuration": typed_configuration.model_dump(mode="json"),
            "filename": file.filename or "script.zip",
            "sha256": hashlib.sha256(file_data).hexdigest(),
            **({"cases": [case.model_dump(mode="json") for case in typed_cases]} if typed_cases else {}),
        },
        response_model=ScriptDefinition,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        record = await service.upload(
            project_id=typed_project_id,
            name=name,
            executor_plugin_id=plugin_id,
            executor_version=version,
            configuration=typed_configuration,
            filename=file.filename or "script.zip",
            file_data=file_data,
            created_by=access.user.persisted_id,
            cases=typed_cases,
        )
    except ScriptDefinitionError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise
    response = record.definition
    complete_idempotency(
        idempotency,
        result.reservation,
        response,
        response_status=status.HTTP_201_CREATED,
    )
    return response


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
        raise HTTPException(status_code=422, detail=" ScriptDefinition ID 不合法") from exc
    if revision is None or revision < 1:
        raise HTTPException(status_code=422, detail="revision 必须大于 0")
    with uow_factory() as uow:
        record = uow.script_definitions.get(definition_id, revision)
    if record is None or record.definition.project_id != typed_project_id:
        raise HTTPException(status_code=404, detail="ScriptDefinition 不存在")
    return record.definition


@router.post(
    "/{project_id}/test-tasks",
    response_model=TaskView,
    status_code=status.HTTP_201_CREATED,
)
def create_task(
    project_id: str,
    body: TaskCreateRequest,
    access: ProjectManagerDep,
    service: TaskServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> TaskView:
    try:
        typed_project_id = BusinessId(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" 项目 ID 不合法") from exc
    if body.task.project_id != typed_project_id:
        raise HTTPException(status_code=422, detail="TestTask 项目与路径不一致")
    result = reserve_idempotency(
        idempotency,
        idempotency_key,
        scope=f"test-task.create:{typed_project_id.root}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=TaskView,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        response = _task_view(service.create_task(body.task, created_by=access.user.persisted_id))
        complete_idempotency(
            idempotency,
            result.reservation,
            response,
            response_status=status.HTTP_201_CREATED,
        )
        return response
    except KeyError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@router.get(
    "/{project_id}/test-tasks",
    response_model=list[TaskView],
)
def list_tasks(
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    enabled: bool | None = None,
) -> list[TaskView]:
    typed_project_id = _project_id(project_id)
    with uow_factory() as uow:
        records = uow.test_tasks.list_by_project(typed_project_id, enabled=enabled)
    return [_task_view(record) for record in records]


@router.get(
    "/{project_id}/test-tasks/{task_id}",
    response_model=TaskView,
)
def get_task(
    project_id: str,
    task_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    revision: int | None = None,
) -> TaskView:
    typed_project_id = _project_id(project_id)
    try:
        typed_task_id = BusinessId(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" TestTask ID 不合法") from exc
    with uow_factory() as uow:
        record = uow.test_tasks.get(typed_task_id, revision)
    if record is None or record.task.project_id != typed_project_id:
        raise HTTPException(status_code=404, detail=" TestTask 不存在")
    return _task_view(record)


@router.post(
    "/{project_id}/runs",
    response_model=RunView,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    project_id: str,
    body: RunCreateRequest,
    _access: ProjectOperatorDep,
    service: TaskServiceDep,
    scheduler: SchedulerServiceDep,
    event_publisher: EventPublisherDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunView:
    try:
        typed_project_id = BusinessId(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" 项目 ID 不合法") from exc
    if body.trigger_type not in {TriggerType.MANUAL_WEB, TriggerType.API}:
        raise HTTPException(status_code=403, detail="retry/recovery Run 只能由 Master 内部服务创建")
    idempotency_result = reserve_idempotency(
        idempotency,
        idempotency_key,
        scope=f"run.create:{typed_project_id.root}:{_access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=RunView,
    )
    if idempotency_result.replayed:
        assert idempotency_result.response is not None
        return idempotency_result.response
    reservation = idempotency_result.reservation
    try:
        created = service.create_run(
            body.task_id,
            project_id=typed_project_id,
            task_revision=body.task_revision,
            trigger_type=body.trigger_type,
            run_id=body.run_id or BusinessId(new_id()),
            original_run_id=body.original_run_id,
            case_filter=set(body.case_filter) if body.case_filter is not None else None,
        )
        schedule = scheduler.schedule_run(BusinessId(created.run.run_id))
        response = _run_view(created, schedule)
        complete_idempotency(
            idempotency,
            reservation,
            response,
            response_status=status.HTTP_201_CREATED,
        )
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
        return response
    except KeyError as exc:
        release_idempotency(idempotency, reservation)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        release_idempotency(idempotency, reservation)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, reservation)
        raise


@router.get(
    "/{project_id}/runs",
    response_model=list[RunListView],
)
def list_runs(
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    limit: int = 100,
    offset: int = 0,
) -> list[RunListView]:
    typed_project_id = _project_id(project_id)
    if not 1 <= limit <= 1000 or offset < 0:
        raise HTTPException(status_code=422, detail="limit/offset 参数不合法")
    with uow_factory() as uow:
        runs = uow.task_runs.list(project_id=typed_project_id.root, limit=limit, offset=offset)
    return [_run_list_view(run) for run in runs if run.snapshot is not None]


@router.get(
    "/{project_id}/runs/{run_id}/logs",
    response_model=list[RunLogView],
)
def list_run_logs(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    after_sequence: int = 0,
) -> list[RunLogView]:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, " Run ID")
    if after_sequence < 0:
        raise HTTPException(status_code=422, detail="after_sequence 不能小于 0")
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None:
            raise HTTPException(status_code=404, detail=" Run 不存在")
        logs = uow.run_logs.list_by_run(run.run_id, after_sequence=after_sequence)
    return [
        RunLogView(
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
    response_model=list[RunEventView],
)
def list_run_events(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> list[RunEventView]:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, " Run ID")
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None:
            raise HTTPException(status_code=404, detail=" Run 不存在")
        events = uow.domain_events.list_by_aggregate(run.run_id, project_id=typed_project_id.root)
    return [
        RunEventView(
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
    response_model=list[RunArtifactView],
)
def list_run_artifacts(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> list[RunArtifactView]:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, " Run ID")
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None:
            raise HTTPException(status_code=404, detail=" Run 不存在")
        artifacts = uow.run_artifacts.list_by_run(run.run_id)
    return [
        RunArtifactView(
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
def download_run_artifact(
    project_id: str,
    run_id: str,
    artifact_id: str,
    _access: ProjectAccessDep,
    artifact_service: ArtifactServiceDep,
) -> StreamingResponse:
    typed_run_id = _business_id(run_id, " Run ID")
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
    response_model=RunDetailView,
)
def get_run(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> RunDetailView:
    typed_project_id = _project_id(project_id)
    try:
        typed_run_id = BusinessId(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" Run ID 不合法") from exc
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None:
            raise HTTPException(status_code=404, detail=" Run 不存在")
        shards = uow.run_shards.list_by_run(run.run_id)
        result = uow.run_results.get_by_run_id(run.run_id)
        case_results = uow.run_case_results.list_by_run(run.run_id)
        artifacts = uow.run_artifacts.list_by_run(run.run_id)
    return _run_detail_view(run, shards, result, case_results, artifacts)


@router.post(
    "/{project_id}/runs/{run_id}/cancel",
    response_model=RunListView,
)
def cancel_run(
    project_id: str,
    run_id: str,
    body: RunActionRequest,
    _access: ProjectOperatorDep,
    uow_factory: UowFactoryDep,
    execution: ExecutionServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunListView:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, " Run ID")
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if run is None or run.snapshot is None:
            raise HTTPException(status_code=404, detail=" Run 不存在")
        plans = uow.execution_plans.list_by_run(typed_run_id)
    idempotency_result = reserve_idempotency(
        idempotency,
        idempotency_key,
        scope=f"run.cancel:{typed_project_id.root}:{_access.user.persisted_id}:{typed_run_id.root}",
        payload={"run_id": typed_run_id.root, "reason": body.reason, "operation": "cancel"},
        response_model=RunListView,
    )
    if idempotency_result.replayed:
        assert idempotency_result.response is not None
        return idempotency_result.response
    reservation = idempotency_result.reservation
    try:
        for plan in plans:
            try:
                execution.request_cancel(plan.plan.plan_id, reason=body.reason)
            except ValueError:
                continue
        response = _run_list_view(run)
        complete_idempotency(idempotency, reservation, response, response_status=status.HTTP_200_OK)
        return response
    except Exception:
        release_idempotency(idempotency, reservation)
        raise


@router.post(
    "/{project_id}/runs/{run_id}/retry",
    response_model=RunView,
    status_code=status.HTTP_201_CREATED,
)
async def retry_run(
    project_id: str,
    run_id: str,
    body: RunActionRequest,
    access: ProjectOperatorDep,
    service: TaskServiceDep,
    scheduler: SchedulerServiceDep,
    event_publisher: EventPublisherDep,
    uow_factory: UowFactoryDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunView:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, " Run ID")
    with uow_factory() as uow:
        source = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if source is None or source.snapshot is None:
            raise HTTPException(status_code=404, detail=" Run 不存在")
    idempotency_result = reserve_idempotency(
        idempotency,
        idempotency_key,
        scope=f"run.retry:{typed_project_id.root}:{access.user.persisted_id}:{typed_run_id.root}",
        payload={"run_id": typed_run_id.root, "reason": body.reason, "operation": "retry"},
        response_model=RunView,
    )
    if idempotency_result.replayed:
        assert idempotency_result.response is not None
        return idempotency_result.response
    reservation = idempotency_result.reservation
    try:
        created = service.create_run(
            BusinessId(source.task_id),
            project_id=typed_project_id,
            task_revision=source.task_revision,
            trigger_type=TriggerType.RETRY,
            run_id=BusinessId(new_id()),
            original_run_id=typed_run_id,
        )
        schedule = scheduler.schedule_run(BusinessId(created.run.run_id))
        response = _run_view(created, schedule)
        complete_idempotency(idempotency, reservation, response, response_status=status.HTTP_201_CREATED)
        await event_publisher.publish(
            "run.created",
            {
                "run_id": created.run.run_id,
                "task_id": created.run.task_id,
                "project_id": typed_project_id.root,
                "original_run_id": typed_run_id.root,
            },
            project_id=typed_project_id.root,
            aggregate_id=created.run.run_id,
        )
        return response
    except Exception:
        release_idempotency(idempotency, reservation)
        raise


@router.post(
    "/{project_id}/runs/{run_id}/retry-failed",
    response_model=RunView,
    status_code=status.HTTP_201_CREATED,
)
async def retry_failed_run(
    project_id: str,
    run_id: str,
    body: RunActionRequest,
    access: ProjectOperatorDep,
    service: TaskServiceDep,
    scheduler: SchedulerServiceDep,
    event_publisher: EventPublisherDep,
    uow_factory: UowFactoryDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RunView:
    typed_project_id = _project_id(project_id)
    typed_run_id = _business_id(run_id, " Run ID")
    with uow_factory() as uow:
        source = uow.task_runs.get_by_run_id(typed_run_id.root, typed_project_id.root)
        if source is None or source.snapshot is None:
            raise HTTPException(status_code=404, detail=" Run 不存在")
        case_results = uow.run_case_results.list_by_run(typed_run_id.root)
    latest_case_results = {}
    for item in case_results:
        current = latest_case_results.get(item.case_key)
        if current is None or item.attempt_no > current.attempt_no:
            latest_case_results[item.case_key] = item
    failed_case_keys = {
        case_key
        for case_key, item in latest_case_results.items()
        if item.status.value in {"failed", "error"}
    }
    if not failed_case_keys:
        raise HTTPException(status_code=409, detail=" Run 没有可重试的失败用例")
    idempotency_result = reserve_idempotency(
        idempotency,
        idempotency_key,
        scope=f"run.retry-failed:{typed_project_id.root}:{access.user.persisted_id}:{typed_run_id.root}",
        payload={
            "run_id": typed_run_id.root,
            "reason": body.reason,
            "operation": "retry-failed",
            "case_keys": sorted(failed_case_keys),
        },
        response_model=RunView,
    )
    if idempotency_result.replayed:
        assert idempotency_result.response is not None
        return idempotency_result.response
    reservation = idempotency_result.reservation
    try:
        created = service.create_run(
            BusinessId(source.task_id),
            project_id=typed_project_id,
            task_revision=source.task_revision,
            trigger_type=TriggerType.RETRY,
            run_id=BusinessId(new_id()),
            original_run_id=typed_run_id,
            case_filter=failed_case_keys,
        )
        schedule = scheduler.schedule_run(BusinessId(created.run.run_id))
        response = _run_view(created, schedule)
        complete_idempotency(idempotency, reservation, response, response_status=status.HTTP_201_CREATED)
        await event_publisher.publish(
            "run.created",
            {
                "run_id": created.run.run_id,
                "task_id": created.run.task_id,
                "project_id": typed_project_id.root,
                "original_run_id": typed_run_id.root,
                "retried_case_keys": sorted(failed_case_keys),
            },
            project_id=typed_project_id.root,
            aggregate_id=created.run.run_id,
        )
        return response
    except Exception:
        release_idempotency(idempotency, reservation)
        raise


def _project_id(value: str) -> BusinessId:
    try:
        return BusinessId(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=" 项目 ID 不合法") from exc


def _business_id(value: str, label: str) -> BusinessId:
    try:
        return BusinessId(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label} 不合法") from exc


__all__ = ["router"]
