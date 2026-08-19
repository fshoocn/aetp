"""项目范围 Run 执行 API（P6.4）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from master.api.v1.dependencies import (
    ArtifactServiceDep,
    EventPublisherDep,
    RunCancelServiceDep,
    RunRetryServiceDep,
    RunTriggerServiceDep,
    UowFactoryDep,
)
from master.api.v1.permissions import ProjectAccessDep, ProjectOperatorDep
from master.api.v1.schemas import (
    RunArtifactOut,
    RunCaseResultOut,
    RunDetailOut,
    RunLogOut,
    RunOut,
    RunTriggerRequest,
    ShardOut,
)

router = APIRouter(
    prefix="/projects/{project_id}/runs",
    tags=["v1-project-runs"],
)


@router.post("", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def trigger_run(
    project_id: str,
    body: RunTriggerRequest,
    access: ProjectOperatorDep,
    trigger_service: RunTriggerServiceDep,
    event_publisher: EventPublisherDep,
) -> RunOut:
    """触发一次任务定义执行（Run）。

    需要项目 operator/maintainer/owner 或平台管理员。触发成功后通过 SSE
    广播 run.created 事件；Agent 执行进度/结果经 SSE run.* 事件推送。
    """
    result = await trigger_service.trigger(
        body.task_id,
        project_id=project_id,
        triggered_by_user_id=access.user.persisted_id,
        case_filter=body.case_filter,
    )
    await event_publisher.publish(
        "run.created",
        {
            "run_id": result.run_id,
            "task_id": result.task_id,
            "project_id": result.project_id,
        },
        project_id=result.project_id,
        aggregate_id=result.run_id,
    )
    return RunOut(
        run_id=result.run_id,
        project_id=result.project_id,
        task_id=result.task_id,
        status="created",
        trigger_type="manual_web",
        created_at=_now(),
    )


@router.get("", response_model=list[RunOut])
def list_runs(
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    limit: int = 100,
    offset: int = 0,
) -> list[RunOut]:
    """列出项目内的 Run（分页，最新在前）。"""
    with uow_factory() as uow:
        runs = uow.task_runs.list(
            project_id=project_id, limit=limit, offset=offset
        )
    return [
        RunOut(
            run_id=run.run_id,
            project_id=run.project_id,
            task_id=run.task_id,
            status=run.status.value,
            trigger_type=run.trigger_type.value,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
        for run in runs
    ]


@router.get("/{run_id}", response_model=RunDetailOut)
def get_run(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
) -> RunDetailOut:
    """查询 Run 详情（含 Shard、汇总结果与 case×attempt 结果矩阵，P7.5）。"""
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(run_id, project_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run 不存在")
        shards = uow.run_shards.list_by_run(run_id)
        result = uow.run_results.get_by_run_id(run_id)
        case_results = uow.run_case_results.list_by_run(run_id)
    return RunDetailOut(
        run_id=run.run_id,
        project_id=run.project_id,
        task_id=run.task_id,
        status=run.status.value,
        trigger_type=run.trigger_type.value,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        shards=[
            ShardOut(
                shard_id=shard.shard_id,
                shard_index=shard.shard_index,
                case_keys=list(shard.case_keys),
                status=shard.status.value,
                final_node=shard.final_node,
            )
            for shard in shards
        ],
        result=(
            {
                "result_id": result.result_id,
                "passed": result.passed,
                "status": result.status.value,
                "node_id": result.node_id,
                "metrics": result.metrics,
                "data": result.data,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
            }
            if result is not None
            else None
        ),
        case_results=[
            RunCaseResultOut(
                run_id=c.run_id,
                shard_id=c.shard_id,
                case_key=c.case_key,
                attempt_no=c.attempt_no,
                status=c.status.value,
                duration_ms=c.duration_ms,
                error_summary=c.error_summary,
                detail=c.detail,
            )
            for c in case_results
        ],
    )


@router.get("/{run_id}/logs", response_model=list[RunLogOut])
def get_run_logs(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    after_sequence: int = 0,
) -> list[RunLogOut]:
    """查询 Run 执行日志（按 sequence 升序，支持增量拉取）。"""
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(run_id, project_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run 不存在")
        logs = uow.run_logs.list_by_run(run_id, after_sequence=after_sequence)
    return [
        RunLogOut(
            id=log.id or 0,
            run_id=log.run_id,
            node_id=log.node_id,
            sequence=log.sequence,
            level=log.level.value,
            message=log.message,
            detail=log.detail,
            occurred_at=log.occurred_at,
        )
        for log in logs
    ]


@router.get("/{run_id}/artifacts", response_model=list[RunArtifactOut])
def list_run_artifacts(
    project_id: str,
    run_id: str,
    _access: ProjectAccessDep,
    artifact_service: ArtifactServiceDep,
) -> list[RunArtifactOut]:
    """列出 Run 的结束产物（报告/日志归档/数据）。"""
    artifacts = artifact_service.list_by_run(run_id, project_id)
    return [RunArtifactOut.model_validate(a) for a in artifacts]


@router.get("/{run_id}/artifacts/{artifact_id}/download")
def download_run_artifact(
    project_id: str,
    run_id: str,
    artifact_id: str,
    _access: ProjectAccessDep,
    artifact_service: ArtifactServiceDep,
) -> StreamingResponse:
    """下载 Run 产物（项目范围，校验 run 归属）。"""
    artifact = artifact_service.get_by_artifact_id(artifact_id, project_id)
    if artifact is None or artifact.run_id != run_id:
        raise HTTPException(status_code=404, detail="产物不存在")
    return StreamingResponse(
        artifact_service.open(artifact),
        media_type="application/octet-stream",
        headers={
            "X-Checksum-Sha256": artifact.sha256,
            "Content-Disposition": (
                f'attachment; filename="{artifact.file_ref.rsplit("/", 1)[-1]}"'
            ),
        },
    )


def _now():
    """创建时间占位（由仓储刷新，这里用 UTC 当前时间兜底）。"""
    from master.domain.time import utcnow

    return utcnow()


@router.post("/{run_id}/retry", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def retry_run(
    project_id: str,
    run_id: str,
    access: ProjectOperatorDep,
    retry_service: RunRetryServiceDep,
    event_publisher: EventPublisherDep,
) -> RunOut:
    """基于失败 Run 完整重跑（新 Run，trigger_type=retry，D-20）。"""
    result = await retry_service.retry(
        run_id,
        project_id=project_id,
        triggered_by_user_id=access.user.persisted_id,
    )
    await event_publisher.publish(
        "run.created",
        {
            "run_id": result.new_run_id,
            "task_id": result.task_id,
            "project_id": result.project_id,
            "original_run_id": result.original_run_id,
        },
        project_id=result.project_id,
        aggregate_id=result.new_run_id,
    )
    return RunOut(
        run_id=result.new_run_id,
        project_id=result.project_id,
        task_id=result.task_id,
        status="created",
        trigger_type="retry",
        created_at=_now(),
    )


@router.post(
    "/{run_id}/retry-failed",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
)
async def retry_failed_run(
    project_id: str,
    run_id: str,
    access: ProjectOperatorDep,
    retry_service: RunRetryServiceDep,
    event_publisher: EventPublisherDep,
) -> RunOut:
    """仅重跑失败 case（新 Run，case 集合=原 Run 失败 case，D-20）。"""
    result = await retry_service.retry_failed(
        run_id,
        project_id=project_id,
        triggered_by_user_id=access.user.persisted_id,
    )
    await event_publisher.publish(
        "run.created",
        {
            "run_id": result.new_run_id,
            "task_id": result.task_id,
            "project_id": result.project_id,
            "original_run_id": result.original_run_id,
            "retried_case_keys": list(result.retried_case_keys),
        },
        project_id=result.project_id,
        aggregate_id=result.new_run_id,
    )
    return RunOut(
        run_id=result.new_run_id,
        project_id=result.project_id,
        task_id=result.task_id,
        status="created",
        trigger_type="retry",
        created_at=_now(),
    )


@router.post("/{run_id}/cancel", response_model=RunOut)
def cancel_run(
    project_id: str,
    run_id: str,
    access: ProjectOperatorDep,
    cancel_service: RunCancelServiceDep,
) -> RunOut:
    """取消一个正在执行的 Run（向活跃 Shard 节点发 run.cancel）。

    Agent 收到取消命令后在安全点释放硬件并报告 cancelled 结果，
    Run 状态由 Agent 结果投影决定（§5.4：Run 无 cancelling 中间态）。
    """
    try:
        cancel_service.cancel(run_id, project_id=project_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    with cancel_service._uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(run_id, project_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run 不存在")
    return RunOut(
        run_id=run.run_id,
        project_id=run.project_id,
        task_id=run.task_id,
        status=run.status.value,
        trigger_type=run.trigger_type.value,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
