"""通用 case 统计 Analyzer（独立 analyzer 插件包）。"""

from __future__ import annotations

from aetp_protocol.reporting import AnalysisRequest, AnalysisResult, PluginContext


class CaseStatisticsAnalyzer:
    plugin_id = "org.case-statistics.analyzer"
    plugin_version = "1.0.0"

    async def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        del context
        cases = request.result.cases
        durations = [case.duration_ms for case in cases if case.duration_ms is not None]
        failed = sum(case.status.value in {"failed", "error"} for case in cases)
        total = len(cases)
        return AnalysisResult(
            metrics={
                "total": total,
                "passed": sum(case.status.value == "passed" for case in cases),
                "failed": failed,
                "skipped": sum(case.status.value == "skipped" for case in cases),
                "duration_ms": sum(durations),
                "failure_rate": failed / total if total else 0.0,
            }
        )


def create_analyzer() -> CaseStatisticsAnalyzer:
    return CaseStatisticsAnalyzer()


__all__ = ["CaseStatisticsAnalyzer", "create_analyzer"]
