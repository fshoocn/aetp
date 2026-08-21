"""Case 成功耗时统计（P6.8，D-21）。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from aetp_protocol.ids import new_id

from master.domain.models import DomainEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DurationUpdate:
    """一次耗时样本处理结果。"""

    accepted: bool
    reason: str = ""
    average_duration_s: float | None = None
    duration_samples: int = 0
    deviation_percent: float | None = None
    anomaly_event: DomainEvent | None = None


class CaseDurationStatsService:
    """按 D-21 规则更新成功 case 的滚动平均耗时。"""

    def __init__(
        self,
        *,
        default_duration_s: float = 60.0,
        anomaly_percent: float = 300.0,
    ) -> None:
        if not math.isfinite(default_duration_s) or default_duration_s <= 0:
            raise ValueError("case_duration_default_s 必须是正数")
        if not math.isfinite(anomaly_percent) or anomaly_percent < 0:
            raise ValueError("case_duration_anomaly_percent 必须是非负数")
        self._default_duration_s = default_duration_s
        self._anomaly_percent = anomaly_percent

    @property
    def default_duration_s(self) -> float:
        """缺少历史耗时时 by-time 分割使用的默认秒数。"""
        return self._default_duration_s

    @property
    def anomaly_percent(self) -> float:
        """允许相对当前均值的最大偏离百分比。"""
        return self._anomaly_percent

    def record_success(
        self,
        uow,
        *,
        script_id: str,
        project_id: str,
        run_id: str,
        shard_id: str,
        attempt_no: int,
        case_key: str,
        duration_ms: int | None,
    ) -> DurationUpdate:
        """记录成功 case 的耗时；缺失、异常或未知 case 不更新统计。"""
        if duration_ms is None:
            return DurationUpdate(False, reason="missing_duration")

        duration_s = duration_ms / 1000.0
        case = uow.script_cases.get_by_stable_key(script_id, case_key)
        if case is None:
            logger.warning(
                "耗时统计找不到脚本用例: run_id=%s script_id=%s case_key=%s",
                run_id,
                script_id,
                case_key,
            )
            return DurationUpdate(False, reason="case_not_found")

        if not math.isfinite(duration_s) or duration_s < 0:
            return self._discard_anomaly(
                uow,
                project_id=project_id,
                run_id=run_id,
                shard_id=shard_id,
                attempt_no=attempt_no,
                case=case,
                duration_s=duration_s,
                deviation_percent=None,
                reason="invalid_duration",
            )

        samples = max(0, int(case.duration_samples or 0))
        average = case.avg_duration_s
        deviation_percent = self._deviation_percent(duration_s, average, samples)
        if deviation_percent is not None and deviation_percent > self._anomaly_percent:
            return self._discard_anomaly(
                uow,
                project_id=project_id,
                run_id=run_id,
                shard_id=shard_id,
                attempt_no=attempt_no,
                case=case,
                duration_s=duration_s,
                deviation_percent=deviation_percent,
                reason="deviation_exceeded",
            )

        if average is None or samples == 0:
            new_average = duration_s
        else:
            new_average = ((average * samples) + duration_s) / (samples + 1)
        case.avg_duration_s = new_average
        case.duration_samples = samples + 1
        uow.script_cases.update(case)
        return DurationUpdate(
            accepted=True,
            average_duration_s=new_average,
            duration_samples=samples + 1,
            deviation_percent=deviation_percent,
        )

    @staticmethod
    def _deviation_percent(
        duration_s: float,
        average: float | None,
        samples: int,
    ) -> float | None:
        """计算相对均值偏离；首个样本不判异常。"""
        if average is None or samples <= 0:
            return None
        if average == 0:
            return 0.0 if duration_s == 0 else math.inf
        return abs(duration_s - average) / abs(average) * 100.0

    def _discard_anomaly(
        self,
        uow,
        *,
        project_id: str,
        run_id: str,
        shard_id: str,
        attempt_no: int,
        case,
        duration_s: float,
        deviation_percent: float | None,
        reason: str,
    ) -> DurationUpdate:
        payload = {
            "run_id": run_id,
            "shard_id": shard_id,
            "attempt_no": attempt_no,
            "script_id": case.script_id,
            "case_id": case.case_id,
            "case_key": case.stable_key,
            "duration_s": duration_s,
            "average_duration_s": case.avg_duration_s,
            "duration_samples": case.duration_samples,
            "deviation_percent": deviation_percent,
            "threshold_percent": self._anomaly_percent,
            "reason": reason,
        }
        event = DomainEvent(
            event_id=new_id(),
            project_id=project_id,
            event_type="case.duration_anomaly",
            aggregate_id=case.case_id or case.stable_key,
            payload=payload,
        )
        uow.domain_events.add(event)
        logger.warning(
            "case 耗时异常，样本已丢弃: run_id=%s case_key=%s duration_s=%s "
            "average_s=%s deviation_percent=%s threshold_percent=%s",
            run_id,
            case.stable_key,
            duration_s,
            case.avg_duration_s,
            deviation_percent,
            self._anomaly_percent,
        )
        return DurationUpdate(
            accepted=False,
            reason=reason,
            average_duration_s=case.avg_duration_s,
            duration_samples=case.duration_samples,
            deviation_percent=deviation_percent,
            anomaly_event=event,
        )
