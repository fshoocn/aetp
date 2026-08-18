"""Master Run 投影服务（P6.4，§9.6 阶段 E 收口）。

接收 Agent 侧上报的结构化事实（ACK / progress / log / result），并把它们
投影到 Run / Shard / Attempt / case 结果 / Run 汇总与日志。Master 只做
「校验 + 落库 + 投影」，不执行报告解析（D-19）。

幂等与围栏：

- ACK：按 (run_id, attempt_no) 幂等，仅当 attempt 未 acked 时推进
  acked → running 过渡由 result 携带状态（这里以 run 级 running 投影为准）；
- progress：覆盖式投影（run 级最新进度，无强幂等约束）；
- log：按 (run_id, sequence) 幂等落库；重复 sequence 跳过；
- result：一个 attempt 只接收一个最终结果——同 (run_id, shard_id,
  attempt_no) 已存在终态结果时静默忽略（D-19）；成功后把 Shard/Attempt
  置终态、释放设备、投影 Run 级汇总（results 表）。

本模块只依赖 UnitOfWork 端口与协议 DTO，不接触 MQTT/HTTP。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from aetp_protocol.logs import RunLogBatch
from aetp_protocol.payloads import (
    RunAckPayload,
    RunCaseStatusPayload,
    RunProgressPayload,
    RunResultPayload,
)

from master.domain.enums import (
    CaseStatus,
    DeviceStatus,
    RunLogLevel,
    RunStatus,
    ShardAttemptStatus,
    ShardStatus,
)
from master.domain.models import RunCaseResult, RunLog, RunResult
from master.domain.repositories import UnitOfWork
from master.domain.state_machine import assert_transition
from master.domain.time import utcnow

logger = logging.getLogger(__name__)

# Run 终态 → 对应 Attempt/Shard 终态（结果状态就是统一命名，§8.4）
_ATTEMPT_TERMINAL = {
    "succeeded": ShardAttemptStatus.SUCCEEDED,
    "failed": ShardAttemptStatus.FAILED,
    "cancelled": ShardAttemptStatus.CANCELLED,
    "timed_out": ShardAttemptStatus.TIMED_OUT,
}
_SHARD_TERMINAL = {
    "succeeded": ShardStatus.SUCCEEDED,
    "failed": ShardStatus.FAILED,
    "cancelled": ShardStatus.CANCELLED,
    "timed_out": ShardStatus.TIMED_OUT,
}
_RUN_TERMINAL = {
    "succeeded": RunStatus.SUCCEEDED,
    "failed": RunStatus.FAILED,
    "cancelled": RunStatus.CANCELLED,
    "timed_out": RunStatus.TIMED_OUT,
}
_CASE_STATUS = {
    "passed": CaseStatus.PASSED,
    "failed": CaseStatus.FAILED,
    "skipped": CaseStatus.SKIPPED,
    "error": CaseStatus.ERROR,
    "pending": CaseStatus.PENDING,
    "running": CaseStatus.RUNNING,
}


@dataclass(frozen=True)
class ProjectionResult:
    """一次投影处理结果（供 dispatcher 决定是否推 SSE）。"""

    handled: bool
    event_type: str = ""
    run_id: str = ""
    project_id: str = ""
    payload: dict | None = None


class RunProjectionService:
    """把 Agent 事实投影到 Run 执行域（纯 UoW 依赖，可单测）。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # -- ACK ----------------------------------------------------------------

    def handle_ack(
        self, node_id: str, payload: RunAckPayload
    ) -> ProjectionResult:
        """ACK 投影：accepted=false 记录拒绝；true 推进 attempt → acked。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(payload.run_id)
            if run is None:
                return ProjectionResult(False)

            # dispatch_id == attempt_id（§8.4）
            attempt = uow.shard_attempts.get_by_attempt_id(payload.dispatch_id)
            if attempt is None:
                return ProjectionResult(False)

            if not payload.accepted:
                if attempt.status not in {
                    ShardAttemptStatus.FAILED,
                    ShardAttemptStatus.CANCELLED,
                }:
                    assert_transition(attempt.status, ShardAttemptStatus.FAILED)
                    attempt.status = ShardAttemptStatus.FAILED
                    attempt.error_code = "RUN_REJECTED"
                    attempt.error_message = payload.reason or "Agent 拒绝执行"
                    attempt.finished_at = utcnow()
                    uow.shard_attempts.update(attempt)
                return ProjectionResult(
                    True,
                    event_type="run.ack",
                    run_id=run.run_id,
                    project_id=run.project_id,
                )

            if attempt.status is ShardAttemptStatus.DISPATCHED:
                assert_transition(attempt.status, ShardAttemptStatus.ACKED)
                attempt.status = ShardAttemptStatus.ACKED
                uow.shard_attempts.update(attempt)

                if run.status is RunStatus.DISPATCHED:
                    assert_transition(run.status, RunStatus.ACKED)
                    run.status = RunStatus.ACKED
                    uow.task_runs.update(run)
                return ProjectionResult(
                    True,
                    event_type="run.ack",
                    run_id=run.run_id,
                    project_id=run.project_id,
                )

            # 已 acked/running/终态：幂等忽略
            return ProjectionResult(False)

    # -- progress -----------------------------------------------------------

    def handle_progress(
        self, node_id: str, payload: RunProgressPayload
    ) -> ProjectionResult:
        """进度投影：run 级覆盖式更新（无持久化列，以 SSE + 日志为主）。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(payload.run_id)
            if run is None:
                return ProjectionResult(False)
        return ProjectionResult(
            True,
            event_type="run.progress",
            run_id=payload.run_id,
            project_id=run.project_id,
            payload={
                "run_id": payload.run_id,
                "sequence": payload.sequence,
                "percent": payload.percent,
                "stage": payload.stage,
                "message": payload.message,
            },
        )

    def handle_case_status(
        self, node_id: str, payload: RunCaseStatusPayload
    ) -> ProjectionResult:
        """case 级状态投影（仅支持实时 case 结果的插件，§8.4）。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(payload.run_id)
            if run is None:
                return ProjectionResult(False)
        return ProjectionResult(
            True,
            event_type="run.case-status",
            run_id=payload.run_id,
            project_id=run.project_id,
            payload=payload.model_dump(mode="json"),
        )

    # -- log ----------------------------------------------------------------

    def handle_log(
        self, node_id: str, payload: RunLogBatch
    ) -> ProjectionResult:
        """日志批投影：按 (run_id, sequence) 幂等落库，重复跳过。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(payload.run_id)
            if run is None:
                return ProjectionResult(False)

            inserted = 0
            for entry in payload.entries:
                if uow.run_logs.exists(payload.run_id, entry.sequence):
                    continue
                uow.run_logs.add(
                    RunLog(
                        run_id=payload.run_id,
                        shard_id=None,
                        node_id=entry.node_id,
                        sequence=entry.sequence,
                        level=RunLogLevel(entry.level.value),
                        message=entry.message,
                        detail=dict(entry.detail or {}),
                        occurred_at=entry.occurred_at,
                    )
                )
                inserted += 1
            if inserted == 0:
                return ProjectionResult(False)
            return ProjectionResult(
                True,
                event_type="run.log",
                run_id=run.run_id,
                project_id=run.project_id,
            )

    # -- result -------------------------------------------------------------

    def handle_result(
        self, node_id: str, payload: RunResultPayload
    ) -> ProjectionResult:
        """最终结果投影：锁定 attempt → 终态 → 设备释放 → Run 汇总。"""
        with self._uow_factory() as uow:
            run = uow.task_runs.get_by_run_id(payload.run_id)
            if run is None:
                return ProjectionResult(False)

            attempt = self._find_attempt(uow, payload.shard_id, payload.attempt_no)
            if attempt is None:
                logger.warning(
                    "run.result 找不到 attempt: run=%s shard=%s attempt=%s",
                    payload.run_id,
                    payload.shard_id,
                    payload.attempt_no,
                )
                return ProjectionResult(False)

            # 一个 attempt 只接收一个最终结果（D-19）
            if attempt.status in {
                ShardAttemptStatus.SUCCEEDED,
                ShardAttemptStatus.FAILED,
                ShardAttemptStatus.CANCELLED,
                ShardAttemptStatus.TIMED_OUT,
            }:
                logger.debug(
                    "run.result 重复（attempt 已终态）: attempt=%s",
                    attempt.attempt_id,
                )
                return ProjectionResult(False)

            attempt_status = _ATTEMPT_TERMINAL.get(payload.status)
            if attempt_status is None:
                return ProjectionResult(False)

            # 状态机：acked → running → 终态（Agent 无独立 running 消息）
            if attempt.status is ShardAttemptStatus.ACKED:
                assert_transition(attempt.status, ShardAttemptStatus.RUNNING)
                attempt.status = ShardAttemptStatus.RUNNING
                attempt.started_at = attempt.started_at or utcnow()
                uow.shard_attempts.update(attempt)
            assert_transition(attempt.status, attempt_status)
            attempt.status = attempt_status
            attempt.finished_at = payload.finished_at or utcnow()
            uow.shard_attempts.update(attempt)

            # Shard 终态 + 释放设备（dispatching → running → 终态）
            shard = uow.run_shards.get_by_shard_id(payload.shard_id)
            if shard is not None:
                shard_status = _SHARD_TERMINAL.get(payload.status)
                if shard_status is not None and shard.status not in {
                    ShardStatus.SUCCEEDED,
                    ShardStatus.FAILED,
                    ShardStatus.CANCELLED,
                    ShardStatus.TIMED_OUT,
                }:
                    if shard.status is not ShardStatus.RUNNING:
                        assert_transition(shard.status, ShardStatus.RUNNING)
                        shard.status = ShardStatus.RUNNING
                        uow.run_shards.update(shard)
                    assert_transition(shard.status, shard_status)
                    shard.status = shard_status
                    shard.final_node = node_id
                    uow.run_shards.update(shard)
                for device_id in attempt.device_ids:
                    device = uow.devices.get_by_id(device_id)
                    if device is not None:
                        device.status = (
                            DeviceStatus.ONLINE
                            if device.online
                            else DeviceStatus.OFFLINE
                        )
                        uow.devices.update(device)

            # case 级结果（D-19：结构化 case 结果由插件分析上报）
            self._persist_case_results(
                uow, run.run_id, payload.shard_id, payload.attempt_no,
                payload.case_results,
            )

            # Run 级汇总投影（一 Run 一行）
            self._project_run_result(uow, run, node_id, payload)

            return ProjectionResult(
                True,
                event_type="run.result",
                run_id=run.run_id,
                project_id=run.project_id,
                payload=payload.model_dump(mode="json"),
            )

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _find_attempt(uow, shard_id: str, attempt_no: int):
        return uow.shard_attempts.get_by_shard_attempt(shard_id, attempt_no)

    def _persist_case_results(
        self, uow, run_id: str, shard_id: str, attempt_no: int, case_results: list
    ) -> None:
        for item in case_results or []:
            case_key = item.get("case_key") or item.get("stable_key") or ""
            if not case_key:
                continue
            status = _CASE_STATUS.get(item.get("status", ""), CaseStatus.ERROR)
            existing = uow.run_case_results.get_by_key(
                run_id, shard_id, case_key, attempt_no
            )
            if existing is not None:
                continue
            uow.run_case_results.add_many(
                [
                    RunCaseResult(
                        run_id=run_id,
                        shard_id=shard_id,
                        case_key=case_key,
                        attempt_no=attempt_no,
                        status=status,
                        duration_ms=item.get("duration_ms"),
                        error_summary=item.get("error_summary"),
                        detail=item.get("detail"),
                    )
                ]
            )

    def _project_run_result(
        self, uow, run, node_id: str, payload: RunResultPayload
    ) -> None:
        """把 Run 推进到终态并 upsert Run 级汇总投影（results 表）。

        状态机：Agent 直接 ack → result，无独立 running 消息。因此进入
        终态前若 Run 仍处 created/dispatched/acked，先经 running 过渡
        （记录 started_at），再迁移到终态，保证迁移合法。
        """
        run_status = _RUN_TERMINAL.get(payload.status)
        shards = uow.run_shards.list_by_run(run.run_id)

        all_terminal = all(
            shard.status
            in {
                ShardStatus.SUCCEEDED,
                ShardStatus.FAILED,
                ShardStatus.CANCELLED,
                ShardStatus.TIMED_OUT,
            }
            for shard in shards
        )

        if run_status is not None and all_terminal:
            if run.status not in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.TIMED_OUT,
                RunStatus.LOST,
            }:
                # 非终态先补齐 running（从 created/dispatched/acked 合法过渡）
                if run.status is not RunStatus.RUNNING:
                    assert_transition(run.status, RunStatus.RUNNING)
                    run.status = RunStatus.RUNNING
                    run.started_at = run.started_at or utcnow()
                    uow.task_runs.update(run)
                assert_transition(run.status, run_status)
                run.status = run_status
                run.finished_at = payload.finished_at or utcnow()
                uow.task_runs.update(run)

        result = uow.run_results.get_by_run_id(run.run_id)
        if result is None:
            result = RunResult(
                result_id=uuid.uuid4().hex,
                run_id=run.run_id,
                project_id=run.project_id,
                task_id=run.task_id,
                node_id=node_id,
                passed=payload.passed,
                status=run_status or RunStatus.FAILED,
                metrics=dict(payload.metrics or {}),
                data=dict(payload.data or {}),
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
            uow.run_results.add(result)
        else:
            result.node_id = node_id
            result.passed = payload.passed
            if run_status is not None:
                result.status = run_status
            result.metrics = dict(payload.metrics or {})
            result.data = dict(payload.data or {})
            result.started_at = run.started_at
            result.finished_at = run.finished_at
            uow.run_results.update(result)
