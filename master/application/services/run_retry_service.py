"""Master Run 重试服务（P6.7，D-20 三层重试语义）。

三层重试的落点（§18.8）：

1. **retry（用户/系统）** = 新建 Run（新 run_id，``trigger_type=retry``，
   ``trigger_context`` 引用原 run_id）；原 Run 终态不迁移；
2. **failover（换节点）** = 同 Run 同 Shard 新建 Attempt（attempt_no 递增，
   排除已失败节点）——由 ``ShardSchedulerService`` 在调度时执行（P4.6）；
3. **case 级重试** = 同 Run 内对该 case 新建 Attempt，``run_case_results``
   按 attempt_no 全量保留（D-20，历史不覆盖）。

本服务只负责 1（retry / retry-failed），复用 ``RunTriggerService`` 生成新
Run；失败 case 的判定基于 ``run_case_results`` 按 ``(case_key, attempt_no)``
取每个 case 的**最新 attempt** 状态，仅 failed/error 视为需重跑。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from master.application.errors import RunNotFoundError
from master.application.services.run_trigger_service import (
    RunTriggerService,
    TriggerResult,
)
from master.domain.enums import CaseStatus, TriggerType
from master.domain.models import RunCaseResult
from master.domain.repositories import UnitOfWork

logger = logging.getLogger(__name__)

# 需重跑的 case 终态（failed/error；passed/skipped 不重跑）
_RETRYABLE_CASE_STATUS = {CaseStatus.FAILED, CaseStatus.ERROR}


@dataclass(frozen=True)
class RetryResult:
    """一次重试的产出。"""

    new_run_id: str
    original_run_id: str
    task_id: str
    project_id: str
    retried_case_keys: tuple[str, ...] = ()


class RunRetryService:
    """基于失败 Run 创建新 Run（retry / retry-failed）。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        trigger_service: RunTriggerService,
    ) -> None:
        self._uow_factory = uow_factory
        self._trigger_service = trigger_service

    async def retry(
        self,
        run_id: str,
        *,
        project_id: str,
        triggered_by_user_id: int | None = None,
    ) -> RetryResult:
        """完整重跑一个 Run（全部 case，新 Run）。"""
        return await self._retry(
            run_id,
            project_id=project_id,
            triggered_by_user_id=triggered_by_user_id,
            only_failed=False,
        )

    async def retry_failed(
        self,
        run_id: str,
        *,
        project_id: str,
        triggered_by_user_id: int | None = None,
    ) -> RetryResult:
        """仅重跑失败 case（case 集合=原 Run 失败 case，新 Run，D-20）。"""
        return await self._retry(
            run_id,
            project_id=project_id,
            triggered_by_user_id=triggered_by_user_id,
            only_failed=True,
        )

    async def _retry(
        self,
        run_id: str,
        *,
        project_id: str,
        triggered_by_user_id: int | None,
        only_failed: bool,
    ) -> RetryResult:
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(run_id, project_id)
            if run is None:
                raise RunNotFoundError(f"Run 不存在或不属于当前项目: {run_id}")

            case_filter: list[str] | None = None
            retried: tuple[str, ...] = ()
            if only_failed:
                failed = self._failed_case_keys(
                    uow.run_case_results.list_by_run(run_id)
                )
                case_filter = sorted(failed)
                retried = tuple(case_filter)

            task_id = run.task_id

        trigger_result: TriggerResult = await self._trigger_service.trigger(
            task_id,
            project_id=project_id,
            triggered_by_user_id=triggered_by_user_id,
            case_filter=case_filter,
            trigger_type=TriggerType.RETRY,
            trigger_context={
                "original_run_id": run_id,
                "mode": "retry_failed" if only_failed else "retry",
            },
        )
        logger.info(
            "Run 重试成功: original=%s new=%s mode=%s",
            run_id,
            trigger_result.run_id,
            "retry_failed" if only_failed else "retry",
        )
        return RetryResult(
            new_run_id=trigger_result.run_id,
            original_run_id=run_id,
            task_id=trigger_result.task_id,
            project_id=trigger_result.project_id,
            retried_case_keys=retried,
        )

    @staticmethod
    def _failed_case_keys(case_results: list[RunCaseResult]) -> set[str]:
        """按 (case_key, attempt_no) 取每个 case 最新 attempt，failed/error 视为失败。

        D-20：历史失败全量保留，但重跑判定只看每个 case 的**最新** attempt
        结果（最新成功则不再重跑）。
        """
        latest: dict[str, RunCaseResult] = {}
        for result in case_results:
            current = latest.get(result.case_key)
            if current is None or result.attempt_no > current.attempt_no:
                latest[result.case_key] = result
        return {
            case_key
            for case_key, result in latest.items()
            if result.status in _RETRYABLE_CASE_STATUS
        }
