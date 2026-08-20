"""Hook 执行审计 API（P8.4，§10.6）。"""

from __future__ import annotations

from fastapi import APIRouter

from master.api.v1.dependencies import HookRunnerDep
from master.api.v1.permissions import ProjectAccessDep
from master.api.v1.schemas import HookExecutionOut

router = APIRouter(
    prefix="/projects/{project_id}/hook-executions",
    tags=["v1-hook-executions"],
)


@router.get("", response_model=list[HookExecutionOut])
def list_hook_executions(
    project_id: str,
    _access: ProjectAccessDep,
    service: HookRunnerDep,
    limit: int = 100,
    offset: int = 0,
) -> list[HookExecutionOut]:
    """查询项目范围的 Hook 执行审计记录。"""
    executions = service.list_executions(project_id, limit=limit, offset=offset)
    return [HookExecutionOut.model_validate(e) for e in executions]
