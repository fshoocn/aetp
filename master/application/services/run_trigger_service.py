"""Master Run 触发服务（P6.4，§18.6/§18.7）。

手动/API 触发一个测试任务定义，产出一次真实执行（Run）：

1. 加载任务定义与引用脚本（项目边界校验）；
2. 从脚本用例索引构建 ``CaseInfo`` 列表，按 case_selection 过滤（D-15）；
3. 调用 Master 插件 ``split_shards`` 分割出 ``ShardSpec`` 列表（§18.6）；
4. 同一事务内创建 ``TaskRun``（固化 script_ref/case_selection/split_policy）
   与 ``RunShard``（pending）；
5. 调用 ``ShardSchedulerService.schedule_run`` 派发（设备分配 + run.assign outbox）。

Run 创建即固化为快照；分割与派发分离，派发失败不撤销 Run（可重调度）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from aetp_protocol.plugin import CaseInfo

from master.application.errors import ScriptNotFoundError, TaskNotFoundError
from master.application.services.shard_scheduler_service import (
    ShardSchedulerService,
)
from master.application.services.case_duration_service import (
    CaseDurationStatsService,
)
from master.domain.enums import RunStatus, ShardStatus, TriggerType
from master.domain.models import RunShard, TaskRun
from master.domain.repositories import UnitOfWork
from master.plugins import PluginRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TriggerResult:
    """一次触发的产出。"""

    run_id: str
    task_id: str
    project_id: str
    shard_ids: tuple[str, ...] = ()
    scheduled: int = 0
    pending_shard_ids: tuple[str, ...] = ()


class RunTriggerService:
    """创建 Run + Shards 并触发派发。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        plugin_registry: PluginRegistry,
        scheduler: ShardSchedulerService,
        duration_stats: CaseDurationStatsService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._plugin_registry = plugin_registry
        self._scheduler = scheduler
        self._duration_stats = duration_stats or CaseDurationStatsService()

    async def trigger(
        self,
        task_id: str,
        *,
        project_id: str,
        triggered_by_user_id: int | None = None,
        case_filter: list[str] | None = None,
        trigger_type: TriggerType = TriggerType.MANUAL_WEB,
        trigger_context: dict | None = None,
    ) -> TriggerResult:
        """触发一次 Run（分割 + 创建 + 派发）。"""
        # 1. 加载上下文（不跨事务保留 ORM，逐段读取）
        with self._uow_factory() as uow:
            task = uow.test_tasks.get_by_task_id(task_id, project_id)
            if task is None:
                raise TaskNotFoundError(
                    f"任务定义不存在或不属于当前项目: {task_id}"
                )
            script = uow.test_scripts.get_by_script_id(task.script_id)
            if (
                script is None
                or script.project_id != project_id
                or script.version != task.script_version
            ):
                raise ScriptNotFoundError(
                    f"脚本版本不存在或不属于当前项目: "
                    f"{task.script_id} v{task.script_version}"
                )
            cases = uow.script_cases.list_by_script(script.script_id)
            task_id = task.task_id
            project_id = task.project_id

        # 2. 构建 CaseInfo（case_selection 覆盖默认，D-15）
        selected = case_filter if case_filter is not None else task.default_case_selection
        # P7.4 兼容语义：空默认用例集合表示该脚本的全部用例。
        selected_set = set(selected) if selected else None
        case_infos: list[CaseInfo] = []
        for case in cases:
            if case.deleted:
                continue
            if selected_set is not None and case.stable_key not in selected_set:
                continue
            case_infos.append(
                CaseInfo(
                    stable_key=case.stable_key,
                    name=case.name,
                    parent_path=case.parent_path,
                    tags=tuple(case.tags or []),
                    params=dict(case.params or {}),
                    estimated_duration_s=case.avg_duration_s,
                )
            )

        # 3. 插件分割（split_shards 为 async 契约）
        package = self._plugin_registry.require(task.task_type)
        split_policy = dict(task.split_policy or {})
        if split_policy.get("type") == "by_time":
            split_policy.setdefault(
                "default_duration_s", self._duration_stats.default_duration_s
            )
        shard_specs = await package.master.split_shards(
            case_infos,
            split_policy,
            dict(script.config or {}),
        )

        # 4. 创建 Run + Shards（同一事务）
        run_id = f"R-{uuid.uuid4().hex.upper()}"
        script_ref = {
            "script_id": script.script_id,
            "version": script.version,
            "sha256": script.sha256,
        }
        with self._uow_factory() as uow:
            run = uow.task_runs.add(
                TaskRun(
                    run_id=run_id,
                    project_id=project_id,
                    task_id=task_id,
                    script_ref=script_ref,
                    case_selection=[c.stable_key for c in case_infos],
                    split_policy=split_policy,
                    trigger_type=trigger_type,
                    triggered_by_user_id=triggered_by_user_id,
                    trigger_context=dict(trigger_context) if trigger_context else None,
                    status=RunStatus.CREATED,
                )
            )
            shards = [
                RunShard(
                    shard_id=f"SH-{uuid.uuid4().hex.upper()}",
                    run_id=run_id,
                    shard_index=index,
                    case_keys=list(spec.case_keys),
                    execution_params=dict(spec.execution_params or {}),
                    estimated_duration_s=spec.estimated_duration_s,
                    status=ShardStatus.PENDING,
                )
                for index, spec in enumerate(shard_specs)
            ]
            if shards:
                uow.run_shards.add_many(shards)

        # 5. 派发（设备分配 + run.assign outbox）
        schedule = self._scheduler.schedule_run(run_id)

        logger.info(
            "Run 触发成功: run_id=%s task=%s shards=%d scheduled=%d pending=%d",
            run_id,
            task_id,
            len(shard_specs),
            len(schedule.scheduled),
            len(schedule.pending_shard_ids),
        )
        return TriggerResult(
            run_id=run_id,
            task_id=task_id,
            project_id=project_id,
            shard_ids=tuple(s.shard_id for s in shards),
            scheduled=len(schedule.scheduled),
            pending_shard_ids=schedule.pending_shard_ids,
        )
