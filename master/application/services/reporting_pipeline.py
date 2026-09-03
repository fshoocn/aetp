"""Master Reporter/Analyzer 扩展点与 Run 结果处理流水线。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from aetp_protocol.artifacts import ArtifactRef
from aetp_protocol.execution import CaseResult, ExecutionResult, ExecutionStatus
from aetp_protocol.ids import BusinessId, Sha256, new_id
from aetp_protocol.plugin_types import PluginPoint
from aetp_protocol.reporting import (
    AnalysisRequest,
    AnalysisResult,
    AnalyzerPlugin,
    PluginContext,
    ReporterPlugin,
    ReportRequest,
    ReportResult,
    UnifiedTestResult,
)

from master.application.services.artifact_storage_service import ArtifactStorageService
from master.domain.enums import RunStatus
from master.domain.models import DomainEvent, RunArtifact, RunCaseResult, RunExtensionResult, RunResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredReporter:
    plugin_id: str
    plugin_version: str
    plugin: ReporterPlugin


@dataclass(frozen=True)
class RegisteredAnalyzer:
    plugin_id: str
    plugin_version: str
    plugin: AnalyzerPlugin


class ReporterRegistry:
    """Master Reporter 注册中心。"""

    def __init__(self, reporters: Iterable[ReporterPlugin] = ()) -> None:
        self._plugins: dict[str, RegisteredReporter] = {}
        for reporter in reporters:
            self.register(reporter)

    def register(
        self,
        reporter: ReporterPlugin,
        *,
        plugin_id: str | None = None,
        plugin_version: str | None = None,
    ) -> None:
        plugin_id, plugin_version = _plugin_identity(
            reporter,
            "reporter",
            plugin_id=plugin_id,
            plugin_version=plugin_version,
        )
        if plugin_id in self._plugins:
            raise ValueError(f"Reporter 已注册: {plugin_id}")
        self._plugins[plugin_id] = RegisteredReporter(plugin_id, plugin_version, reporter)

    def list(self) -> tuple[RegisteredReporter, ...]:
        return tuple(self._plugins.values())


class AnalyzerRegistry:
    """Master Analyzer 注册中心。"""

    def __init__(self, analyzers: Iterable[AnalyzerPlugin] = ()) -> None:
        self._plugins: dict[str, RegisteredAnalyzer] = {}
        for analyzer in analyzers:
            self.register(analyzer)

    def register(
        self,
        analyzer: AnalyzerPlugin,
        *,
        plugin_id: str | None = None,
        plugin_version: str | None = None,
    ) -> None:
        plugin_id, plugin_version = _plugin_identity(
            analyzer,
            "analyzer",
            plugin_id=plugin_id,
            plugin_version=plugin_version,
        )
        if plugin_id in self._plugins:
            raise ValueError(f"Analyzer 已注册: {plugin_id}")
        self._plugins[plugin_id] = RegisteredAnalyzer(plugin_id, plugin_version, analyzer)

    def list(self) -> tuple[RegisteredAnalyzer, ...]:
        return tuple(self._plugins.values())


class _RunArtifactContext(PluginContext):
    def __init__(self, storage: ArtifactStorageService, artifacts: Iterable[RunArtifact]) -> None:
        self._storage = storage
        self._artifacts = {artifact.artifact_id: artifact for artifact in artifacts}

    async def read_artifact(self, artifact: ArtifactRef) -> bytes:
        source = self._artifacts.get(artifact.artifact_id.root)
        if source is None:
            raise FileNotFoundError(f"Run Artifact 不存在: {artifact.artifact_id.root}")

        def read() -> bytes:
            with self._storage.open(source.file_ref) as stream:
                return stream.read()

        return await asyncio.to_thread(read)


class ReportPipeline:
    """在 Run 事实提交后执行 Reporter/Analyzer。"""

    def __init__(
        self,
        uow_factory,
        storage: ArtifactStorageService,
        reporters: ReporterRegistry | None = None,
        analyzers: AnalyzerRegistry | None = None,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._reporters = reporters or ReporterRegistry()
        self._analyzers = analyzers or AnalyzerRegistry()
        self._timeout_s = timeout_s

    @property
    def reporters(self) -> ReporterRegistry:
        return self._reporters

    @property
    def analyzers(self) -> AnalyzerRegistry:
        return self._analyzers

    async def process(self, event: DomainEvent) -> None:
        """处理已持久化的 Run 完成事件；扩展失败不影响 Run。"""
        if event.event_type not in {"run.result", "run.finished"}:
            return
        run_id = _event_run_id(event)
        if run_id is None:
            logger.warning("Reporter 跳过无效 Run 事件: event=%s", event.event_id)
            return
        snapshot = self._snapshot(run_id)
        if snapshot is None:
            logger.warning("Reporter 找不到 Run 结果: run=%s", run_id)
            return
        result, artifacts, request = snapshot
        context = _RunArtifactContext(self._storage, artifacts)
        unified = _unified_from_execution(request.execution_result, run_id)

        for registration in self._reporters.list():
            report_result = await self._run_reporter(registration, request, context, result.run_id)
            if report_result is None:
                continue
            if report_result.result is not None:
                unified = report_result.result
                break

        if unified is not None:
            await self._run_analyzers(unified, context, result.run_id)

    async def _run_reporter(
        self,
        registration: RegisteredReporter,
        request: ReportRequest,
        context: PluginContext,
        run_id: str,
    ) -> ReportResult | None:
        existing = self._get_extension(run_id, "reporter", registration.plugin_id, registration.plugin_version)
        if existing is not None and existing.status == "succeeded":
            if existing.result is None:
                return ReportResult()
            return ReportResult.model_validate(existing.result)
        try:
            value = await asyncio.wait_for(
                registration.plugin.report(request, context),
                timeout=self._timeout_s,
            )
            if not isinstance(value, ReportResult):
                raise TypeError("Reporter 必须返回 ReportResult")
            if value.result is not None and value.result.run_id.root != run_id:
                raise ValueError("Reporter 结果的 run_id 与请求不一致")
        except Exception as exc:  # noqa: BLE001 - 扩展失败不能回滚 Run
            logger.exception("Reporter 执行失败: plugin=%s run=%s", registration.plugin_id, run_id)
            self._save_extension(
                run_id,
                "reporter",
                registration.plugin_id,
                registration.plugin_version,
                status="failed",
                error_message=str(exc)[:1024],
            )
            return None
        self._save_extension(
            run_id,
            "reporter",
            registration.plugin_id,
            registration.plugin_version,
            status="succeeded",
            result=value.model_dump(mode="json"),
            derived_artifact_ids=[item.artifact_id.root for item in value.derived_artifacts],
        )
        return value

    async def _run_analyzers(
        self,
        unified: UnifiedTestResult,
        context: PluginContext,
        run_id: str,
    ) -> None:
        request = AnalysisRequest(run_id=unified.run_id, result=unified)
        for registration in self._analyzers.list():
            existing = self._get_extension(run_id, "analyzer", registration.plugin_id, registration.plugin_version)
            if existing is not None and existing.status == "succeeded":
                continue
            try:
                value = await asyncio.wait_for(
                    registration.plugin.analyze(request, context),
                    timeout=self._timeout_s,
                )
                if not isinstance(value, AnalysisResult):
                    raise TypeError("Analyzer 必须返回 AnalysisResult")
            except Exception as exc:  # noqa: BLE001 - 扩展失败不能回滚 Run
                logger.exception("Analyzer 执行失败: plugin=%s run=%s", registration.plugin_id, run_id)
                self._save_extension(
                    run_id,
                    "analyzer",
                    registration.plugin_id,
                    registration.plugin_version,
                    status="failed",
                    error_message=str(exc)[:1024],
                )
                continue
            self._save_extension(
                run_id,
                "analyzer",
                registration.plugin_id,
                registration.plugin_version,
                status="succeeded",
                result=value.model_dump(mode="json"),
                derived_artifact_ids=[item.artifact_id.root for item in value.derived_artifacts],
            )

    def _snapshot(self, run_id: str) -> tuple[RunResult, list[RunArtifact], ReportRequest] | None:
        with self._uow_factory() as uow:
            result = uow.run_results.get_by_run_id(run_id)
            if result is None:
                return None
            cases = uow.run_case_results.list_by_run(run_id)
            artifacts = uow.run_artifacts.list_by_run(run_id)
        run_id_value = _business_id(result.run_id)
        if run_id_value is None:
            return None
        artifact_refs = tuple(
            ref
            for artifact in artifacts
            if (ref := _artifact_ref(artifact, result.project_id)) is not None
        )
        case_results = tuple(
            case_result
            for case in cases
            if (case_result := _case_result(case)) is not None
        )
        try:
            execution_result = ExecutionResult(
                status=_execution_status(result.status),
                passed=result.passed,
                case_results=case_results,
                artifacts=artifact_refs,
                metrics=dict(result.metrics or {}),
                data=dict(result.data or {}),
            )
        except Exception:
            logger.exception("Run 结果无法转换为 Reporter 请求: run=%s", run_id)
            return None
        return result, artifacts, ReportRequest(
            run_id=run_id_value,
            artifacts=artifact_refs,
            execution_result=execution_result,
        )

    def _get_extension(
        self,
        run_id: str,
        extension_point: str,
        plugin_id: str,
        plugin_version: str,
    ) -> RunExtensionResult | None:
        with self._uow_factory() as uow:
            return uow.run_extension_results.get(run_id, extension_point, plugin_id, plugin_version)

    def _save_extension(
        self,
        run_id: str,
        extension_point: str,
        plugin_id: str,
        plugin_version: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        derived_artifact_ids: list[str] | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._uow_factory() as uow:
            current = uow.run_extension_results.get(run_id, extension_point, plugin_id, plugin_version)
            value = RunExtensionResult(
                id=current.id if current is not None else None,
                extension_id=current.extension_id if current is not None else new_id(),
                run_id=run_id,
                extension_point=extension_point,
                plugin_id=plugin_id,
                plugin_version=plugin_version,
                status=status,
                result=result,
                derived_artifact_ids=derived_artifact_ids or [],
                error_message=error_message,
            )
            if current is None:
                uow.run_extension_results.add(value)
            else:
                uow.run_extension_results.update(value)


def build_default_reporting_registries(resolver=None) -> tuple[ReporterRegistry, AnalyzerRegistry]:
    from plugins.case_statistics_analyzer.master import CaseStatisticsAnalyzer
    from plugins.junit_reporter.master import JUnitReporter

    reporters = ReporterRegistry((JUnitReporter(),))
    analyzers = AnalyzerRegistry((CaseStatisticsAnalyzer(),))
    if resolver is not None:
        for extension in resolver.resolve_all(PluginPoint.REPORTER):
            reporters.register(
                extension.plugin,
                plugin_id=extension.plugin_id,
                plugin_version=extension.plugin_version,
            )
        for extension in resolver.resolve_all(PluginPoint.ANALYZER):
            analyzers.register(
                extension.plugin,
                plugin_id=extension.plugin_id,
                plugin_version=extension.plugin_version,
            )
    return reporters, analyzers


def _plugin_identity(
    plugin: object,
    point: str,
    *,
    plugin_id: str | None = None,
    plugin_version: str | None = None,
) -> tuple[str, str]:
    plugin_id = plugin_id or getattr(plugin, "plugin_id", None)
    plugin_version = plugin_version or getattr(plugin, "plugin_version", None)
    if not isinstance(plugin_id, str) or not plugin_id.strip():
        raise ValueError(f"{point} 插件缺少 plugin_id")
    if not isinstance(plugin_version, str) or not plugin_version.strip():
        raise ValueError(f"{point} 插件缺少 plugin_version")
    return plugin_id, plugin_version


def _event_run_id(event: DomainEvent) -> str | None:
    value = event.payload.get("run_id") or event.aggregate_id
    return value if isinstance(value, str) and _business_id(value) is not None else None


def _business_id(value: str) -> BusinessId | None:
    try:
        return BusinessId(value)
    except ValueError:
        return None


def _execution_status(status: RunStatus) -> ExecutionStatus:
    return {
        RunStatus.SUCCEEDED: ExecutionStatus.SUCCEEDED,
        RunStatus.CANCELLED: ExecutionStatus.CANCELLED,
        RunStatus.TIMED_OUT: ExecutionStatus.TIMED_OUT,
    }.get(status, ExecutionStatus.FAILED)


def _case_result(value: RunCaseResult) -> CaseResult | None:
    try:
        return CaseResult(
            case_key=value.case_key,
            status=value.status,
            duration_ms=value.duration_ms,
            error_summary=value.error_summary,
            detail=value.detail,
        )
    except ValueError:
        logger.warning("忽略无法转换的 Run case 结果: run=%s case=%s", value.run_id, value.case_key)
        return None


def _artifact_ref(value: RunArtifact, project_id: str) -> ArtifactRef | None:
    try:
        return ArtifactRef(
            artifact_id=BusinessId(value.artifact_id),
            project_id=BusinessId(project_id),
            run_id=BusinessId(value.run_id),
            shard_id=BusinessId(value.shard_id) if value.shard_id else None,
            attempt_id=BusinessId(value.attempt_id) if value.attempt_id else None,
            node_id=BusinessId(value.node_id) if value.node_id else None,
            kind=value.kind,
            filename=value.filename,
            content_type=value.content_type,
            size=value.size,
            sha256=Sha256(value.sha256),
            derived_from=BusinessId(value.derived_from) if value.derived_from else None,
        )
    except ValueError:
        logger.warning("忽略无法转换的 Run Artifact: %s", value.artifact_id)
        return None


def _unified_from_execution(execution: ExecutionResult | None, run_id: str) -> UnifiedTestResult | None:
    if execution is None:
        return None
    business_id = _business_id(run_id)
    if business_id is None:
        return None
    return UnifiedTestResult(
        run_id=business_id,
        status=execution.status,
        passed=execution.passed,
        cases=execution.case_results,
        metrics=execution.metrics,
        data=execution.data,
    )


__all__ = [
    "AnalyzerRegistry",
    "ReportPipeline",
    "ReporterRegistry",
    "build_default_reporting_registries",
]
