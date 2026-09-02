"""pytest JUnit XML Master Reporter。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from aetp_protocol.artifacts import ArtifactKind
from aetp_protocol.execution import CaseResult, CaseStatus, ExecutionStatus
from aetp_protocol.ids import JsonValue
from aetp_protocol.reporting import (
    PluginContext,
    ReportRequest,
    ReportResult,
    UnifiedTestResult,
)


class JUnitReporter:
    plugin_id = "org.junit.reporter"
    plugin_version = "1.0.0"

    async def report(self, request: ReportRequest, context: PluginContext) -> ReportResult:
        artifact = next(
            (
                item
                for item in request.artifacts
                if item.kind is ArtifactKind.REPORT
                and (item.filename.lower().endswith(".xml") or "xml" in item.content_type.lower())
            ),
            None,
        )
        if artifact is None:
            return ReportResult()

        root = ET.fromstring(await context.read_artifact(artifact))
        candidates = (
            tuple(item.case_key for item in request.execution_result.case_results)
            if request.execution_result is not None
            else ()
        )
        cases = tuple(self._case_result(case, candidates) for case in root.iter("testcase"))
        failed = sum(case.status in {CaseStatus.FAILED, CaseStatus.ERROR} for case in cases)
        base_status = (
            request.execution_result.status
            if request.execution_result is not None
            else ExecutionStatus.SUCCEEDED
        )
        status = base_status if base_status is not ExecutionStatus.SUCCEEDED or failed == 0 else ExecutionStatus.FAILED
        result = UnifiedTestResult(
            run_id=request.run_id,
            status=status,
            passed=status is ExecutionStatus.SUCCEEDED,
            cases=cases,
            metrics={
                "total": len(cases),
                "passed": sum(case.status is CaseStatus.PASSED for case in cases),
                "failed": failed,
                "skipped": sum(case.status is CaseStatus.SKIPPED for case in cases),
            },
            data={"source": "junit"},
        )
        return ReportResult(result=result)

    @classmethod
    def _case_result(cls, case: ET.Element, candidates: tuple[str, ...]) -> CaseResult:
        name = case.attrib.get("name", "")
        classname = case.attrib.get("classname", "")
        key = cls._case_key(classname, name, candidates)
        status = CaseStatus.PASSED
        if case.find("failure") is not None:
            status = CaseStatus.FAILED
        elif case.find("error") is not None:
            status = CaseStatus.ERROR
        elif case.find("skipped") is not None:
            status = CaseStatus.SKIPPED
        duration_ms = max(0, int(float(case.attrib.get("time", "0")) * 1000))
        detail: dict[str, JsonValue] = {}
        for output_name in ("system-out", "system-err"):
            output = case.findtext(output_name, default="")
            if output.strip():
                detail[output_name] = output
        skipped = case.find("skipped")
        if skipped is not None and skipped.attrib.get("message"):
            detail["skip_reason"] = skipped.attrib["message"]
        error_summary = None
        for outcome_name in ("failure", "error"):
            outcome = case.find(outcome_name)
            if outcome is not None:
                error_summary = "".join(outcome.itertext()).strip() or outcome.attrib.get("message")
                break
        return CaseResult(
            case_key=key,
            status=status,
            duration_ms=duration_ms,
            error_summary=error_summary,
            detail=detail or None,
        )

    @staticmethod
    def _case_key(classname: str, name: str, candidates: tuple[str, ...]) -> str:
        for candidate in candidates:
            if candidate.rsplit("::", 1)[-1] == name and (
                not classname or Path(candidate.split("::", 1)[0]).stem in classname
            ):
                return candidate
        return f"{classname}::{name}".strip(":")


def create_reporter() -> JUnitReporter:
    return JUnitReporter()


__all__ = ["JUnitReporter", "create_reporter"]
