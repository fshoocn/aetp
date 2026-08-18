"""项目范围 Run 执行 API（P6.4）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from master.api.v1.dependencies import (
    EventBusDep,
    RunTriggerServiceDep,
    UowFactoryDep,
)
from master.api.v1.permissions import ProjectAccessDep, ProjectOperatorDep
from master.api.v1.schemas import (
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
    event_bus: EventBusDep,
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
    await event_bus.publish(
        "run.created",
        {
            "run_id": result.run_id,
            "task_id": result.task_id,
            "project_id": result.project_id,
        },
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
    """查询 Run 详情（含 Shard 与汇总结果）。"""
    with uow_factory() as uow:
        run = uow.task_runs.get_by_run_id(run_id, project_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run 不存在")
        shards = uow.run_shards.list_by_run(run_id)
        result = uow.run_results.get_by_run_id(run_id)
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


def _now():
    """创建时间占位（由仓储刷新，这里用 UTC 当前时间兜底）。"""
    from master.domain.time import utcnow

    return utcnow()
