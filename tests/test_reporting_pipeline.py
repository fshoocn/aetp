"""Master Reporter/Analyzer 流水线测试。"""

from __future__ import annotations

import asyncio
import io
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from master.application.services.artifact_storage_service import ArtifactStorageService
from master.application.services.reporting_pipeline import (
    AnalyzerRegistry,
    ReporterRegistry,
    ReportPipeline,
)
from master.domain.enums import ArtifactKind, CaseStatus, RunStatus
from master.domain.models import DomainEvent, RunArtifact, RunCaseResult, RunResult
from plugins.pytest_plugin.master import CaseStatisticsAnalyzer, JUnitReporter

RUN_ID = "01J00000000000000000000001"
PROJECT_ID = "01J00000000000000000000002"
ARTIFACT_ID = "01J00000000000000000000003"


class _ExtensionRepository:
    def __init__(self) -> None:
        self.values = []

    def get(self, run_id, extension_point, plugin_id, plugin_version):
        return next(
            (
                value
                for value in self.values
                if value.run_id == run_id
                and value.extension_point == extension_point
                and value.plugin_id == plugin_id
                and value.plugin_version == plugin_version
            ),
            None,
        )

    def add(self, value):
        self.values.append(value)
        return value

    def update(self, value):
        for index, current in enumerate(self.values):
            if current.id == value.id:
                self.values[index] = value
                return value
        raise AssertionError("扩展结果不存在")


class _Uow(AbstractContextManager):
    def __init__(self, result, cases, artifacts, extensions) -> None:
        self.run_results = _RunResultRepository(result)
        self.run_case_results = _CaseRepository(cases)
        self.run_artifacts = _ArtifactRepository(artifacts)
        self.run_extension_results = extensions

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _RunResultRepository:
    def __init__(self, result) -> None:
        self.result = result

    def get_by_run_id(self, run_id):
        return self.result if self.result.run_id == run_id else None


class _CaseRepository:
    def __init__(self, cases) -> None:
        self.cases = cases

    def list_by_run(self, run_id):
        return [case for case in self.cases if case.run_id == run_id]


class _ArtifactRepository:
    def __init__(self, artifacts) -> None:
        self.artifacts = artifacts

    def list_by_run(self, run_id):
        return [artifact for artifact in self.artifacts if artifact.run_id == run_id]


class _Storage:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def open(self, key: str):
        assert key == "artifacts/report.xml"
        return io.BytesIO(self.data)

    def put(self, key: str, data: bytes) -> None:
        del key, data

    def exists(self, key: str) -> bool:
        return key == "artifacts/report.xml"

    def delete(self, key: str) -> None:
        del key

    def list_keys(self, prefix: str = "") -> list[str]:
        return ["artifacts/report.xml"] if "artifacts/report.xml".startswith(prefix) else []


class _StorageUowFactory:
    def __init__(self, uow) -> None:
        self.uow = uow

    def __call__(self):
        return self.uow


def _pipeline(xml: bytes):
    result = RunResult(
        result_id="01J00000000000000000000004",
        run_id=RUN_ID,
        project_id=PROJECT_ID,
        task_id="task-1",
        passed=True,
        status=RunStatus.SUCCEEDED,
        metrics={"total": 1},
        data={"source": "raw"},
    )
    cases = [
        RunCaseResult(
            run_id=RUN_ID,
            shard_id="01J00000000000000000000005",
            case_key="test_sample.py::test_ok",
            attempt_no=1,
            status=CaseStatus.PASSED,
            duration_ms=10,
        )
    ]
    artifacts = [
        RunArtifact(
            artifact_id=ARTIFACT_ID,
            run_id=RUN_ID,
            kind=ArtifactKind.REPORT,
            file_ref="artifacts/report.xml",
            filename="report.xml",
            content_type="application/xml",
            size=len(xml),
            sha256="a" * 64,
        )
    ]
    extensions = _ExtensionRepository()
    uow = _Uow(result, cases, artifacts, extensions)
    pipeline = ReportPipeline(
        _StorageUowFactory(uow),
        ArtifactStorageService(_Storage(xml)),
        ReporterRegistry((JUnitReporter(),)),
        AnalyzerRegistry((CaseStatisticsAnalyzer(),)),
    )
    return pipeline, result, extensions


def test_junit_reporter_and_analyzer_are_master_extensions() -> None:
    pipeline, original, extensions = _pipeline(
        b'<testsuite><testcase classname="test_sample" name="test_ok" time="0.010" /></testsuite>'
    )
    event = DomainEvent(
        event_id="01J00000000000000000000006",
        project_id=PROJECT_ID,
        event_type="run.result",
        aggregate_id=RUN_ID,
        payload={"run_id": RUN_ID},
        occurred_at=datetime.now(UTC),
    )

    asyncio.run(pipeline.process(event))

    reporter = next(item for item in extensions.values if item.extension_point == "reporter")
    analyzer = next(item for item in extensions.values if item.extension_point == "analyzer")
    assert reporter.status == "succeeded"
    assert reporter.result["result"]["data"]["source"] == "junit"
    assert analyzer.status == "succeeded"
    assert analyzer.result["metrics"]["total"] == 1
    assert analyzer.result["metrics"]["duration_ms"] == 10
    assert original.data == {"source": "raw"}
