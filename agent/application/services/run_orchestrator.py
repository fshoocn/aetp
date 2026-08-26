"""Agent 执行编排器（P6.4，§9.6 阶段 E 收口）。

``RunOrchestrator`` 把 P6.1 的执行器、P6.2 的任务上下文与共享插件包的
Agent 执行面串成一次完整执行闭环：

1. claim 成功并回 ACK 后，CommandDispatcher 把 ``RunAssignPayload`` 交给
   ``start()``，后台执行（不阻塞命令处理）；
2. 构建 ``TaskContext``（进度/日志/case-status 上报 + 取消信号）；
3. 经 ``ExecutionService.execute`` 执行插件（并发上限/超时/取消）；
4. 执行结束：``collect_logs`` → flush spool → 以 ``run.log`` 分批上报并
   标记已发布；调用 ``analyze_results`` 产出结构化 case 结果；
5. 组装 ``RunResultPayload`` 写入本地 outbox（稳定 ID，QoS 1 可靠重放）。

一个 attempt 只上报一个最终结果（D-19）：outbox ID 稳定为
``result:{run_id}:{attempt_no}``，重复执行/重放不会产生第二个 result。

本模块只依赖 Ledger/Transport 端口与协议 DTO，不接触 FastAPI。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aetp_protocol.envelope import Envelope, Sender, SenderKind
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import (
    CaseResultEntry,
    RunAssignPayload,
    RunLogCompletePayload,
    RunResultPayload,
)
from aetp_protocol.topics import event_topic

from agent.application.services.artifact_upload_service import ArtifactUploadService
from agent.application.services.execution_service import ExecutionService
from agent.application.services.script_archive import extract_zip_safely
from agent.application.services.script_cache_service import ScriptCacheService
from agent.application.services.task_context import TaskContext
from agent.config import AgentSettings
from agent.domain.enums import AgentRunStatus
from agent.domain.ledger import Ledger

if TYPE_CHECKING:
    from agent.plugins import AgentPluginRegistry

logger = logging.getLogger(__name__)


def _status_value(status: AgentRunStatus) -> str:
    """把 Agent 本地执行状态映射为 run.result 的 status 值。"""
    return status.value


@dataclass
class _Analysis:
    """analyze_results 的归一化结果。"""

    failed: bool
    error: str = ""
    case_results: list[CaseResultEntry] = field(default_factory=list)
    passed: bool | None = None
    metrics: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping) -> _Analysis:
        raw_cases = mapping.get("case_results") or []
        case_results: list[CaseResultEntry] = []
        for item in raw_cases:
            if isinstance(item, CaseResultEntry):
                case_results.append(item)
                continue
            if isinstance(item, Mapping):
                case_results.append(CaseResultEntry.model_validate(dict(item)))
                continue
            raise ValueError(f"case_results 条目类型非法: {type(item).__name__}")
        passed = mapping.get("passed")
        return cls(
            failed=False,
            case_results=case_results,
            passed=None if passed is None else bool(passed),
            metrics=dict(mapping.get("metrics") or {}),
            data=dict(mapping.get("data") or {}),
        )


class RunOrchestrator:
    """把一次已 claim 的 Run 从执行推进到 result 上报。"""

    def __init__(
        self,
        settings: AgentSettings,
        ledger: Ledger,
        execution_service: ExecutionService,
        plugin_registry: AgentPluginRegistry | None = None,
        *,
        script_cache: ScriptCacheService | None = None,
        artifact_uploader: ArtifactUploadService | None = None,
        session_id: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._execution_service = execution_service
        self._plugin_registry = plugin_registry
        self._script_cache = script_cache
        self._artifact_uploader = artifact_uploader
        self._session_id = session_id or (lambda: settings.node_id)
        self._now = now or (lambda: datetime.now(UTC))
        self._tasks: set[asyncio.Task[None]] = set()
        # 本次 Run 解包出的临时脚本目录，Run 结束后清理
        self._script_dirs: set[Path] = set()

    # -- 入口 ---------------------------------------------------------------

    def start(self, payload: RunAssignPayload) -> asyncio.Task[None]:
        """在 claim 成功后启动后台执行；返回跟踪任务。"""
        task = asyncio.create_task(self._run(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- 执行闭环 -----------------------------------------------------------

    async def _run(self, payload: RunAssignPayload) -> None:
        run_id = payload.run_id
        plugin = self._resolve_plugin(payload)
        context = self._build_context(payload)

        # 异步解包脚本（避免阻塞事件循环）
        if self._script_cache is not None:
            script_ref = dict(payload.script_ref or {})
            script_ref = await asyncio.to_thread(self._enrich_script_ref, script_ref)
            context = self._build_context_with_ref(payload, script_ref)

        try:
            result = await self._execution_service.execute(
                run_id,
                plugin,
                context,
                timeout_s=payload.timeout_s,
            )
            artifact_refs = await self._upload_artifacts(payload, result)
            await self._finalize(payload, plugin, context, result, artifact_refs)
        except Exception:
            logger.exception(
                "Run 执行编排异常: run_id=%s attempt=%s",
                run_id,
                payload.attempt_no,
            )
            await self._report_abort(payload)
        finally:
            self._cleanup_script_dirs()

    # -- 插件解析 -----------------------------------------------------------

    def _resolve_plugin(self, payload: RunAssignPayload):
        """从注册表解析执行插件；缺失由 CommandDispatcher 已在 claim 前拒绝。"""
        if self._plugin_registry is None:
            raise RuntimeError("Agent 插件注册表未装配")
        return self._plugin_registry.require(payload.task_type)

    def _build_context(self, payload: RunAssignPayload) -> TaskContext:
        """构造执行上下文（脚本路径未经解包，异步阶段会替换）。"""
        return self._build_context_with_ref(payload, dict(payload.script_ref or {}))

    def _build_context_with_ref(self, payload: RunAssignPayload, script_ref: dict) -> TaskContext:
        """构造执行上下文；params 取 Shard 专属执行参数（§8.4）。"""
        run_id = payload.run_id
        return TaskContext(
            self._settings,
            self._ledger,
            project_id=payload.project_id,
            task_id=payload.task_id,
            shard_id=payload.shard_id,
            run_id=run_id,
            node_id=self._settings.node_id,
            params={**dict(payload.task_config or {}), **dict(payload.execution_params or {})},
            script_ref=script_ref,
            case_keys=list(payload.case_keys),
            is_cancelled=lambda: self._execution_service.is_cancelled(run_id),
            session_id=self._session_id,
            now=self._now,
        )

    def _enrich_script_ref(self, script_ref: dict) -> dict:
        """把脚本包解包成目录并注入 script_ref.path，供插件定位脚本（§9.8）。

        脚本缓存存的是**单个文件**（zip 或 .py 包），而插件 ``execute`` 期望
        ``script_dir`` 是**解压后的目录**（含 test_*.py 等）。这里复用
        ``ScriptCacheService`` 确保缓存后解包到临时目录，把目录路径注入
        ``script_ref["path"]``；Run 结束后由 ``_cleanup_script_dirs`` 清理。
        """
        if self._script_cache is None:
            return script_ref
        try:
            entry = self._script_cache.ensure_cached(script_ref)
            source = Path(entry.path)
            if not source.is_file():
                logger.warning("缓存脚本文件缺失: %s", source)
                return script_ref
            workspace = Path(self._settings.script_cache_dir).resolve().parent / "runs"
            workspace.mkdir(parents=True, exist_ok=True)
            tmp_dir = Path(tempfile.mkdtemp(prefix="aetp-run-script-", dir=str(workspace)))
            self._extract_script_sync(source, tmp_dir)
            self._script_dirs.add(tmp_dir)
            script_ref["path"] = str(tmp_dir)
        except Exception as exc:  # noqa: BLE001 - 解包失败不阻塞执行
            logger.warning("脚本解包失败，回退缓存文件路径: %s", exc)
            try:
                cached = self._ledger.get_cached_script(
                    script_ref.get("script_id", ""),
                    script_ref.get("version", 1),
                    script_ref.get("sha256", ""),
                )
                if cached is not None:
                    script_ref["path"] = cached.path
            except Exception:  # noqa: BLE001, S110 - 脚本缓存查询失败不阻断执行，静默降级
                pass
        return script_ref

    @staticmethod
    def _extract_script_sync(source: Path, tmp_dir: Path) -> None:
        """同步解包脚本（zip 解压或单文件复制）。"""
        if zipfile.is_zipfile(source):
            extract_zip_safely(source, tmp_dir)
        else:
            shutil.copy2(source, tmp_dir / "test_script.py")

    def _cleanup_script_dirs(self) -> None:
        """清理本次 Run 解包出的临时脚本目录。"""
        for path in self._script_dirs:
            shutil.rmtree(path, ignore_errors=True)
        self._script_dirs.clear()

    # -- 收尾：日志 + 结果 ---------------------------------------------------

    async def _upload_artifacts(self, payload: RunAssignPayload, result) -> list[dict]:
        """上传插件执行结果声明的本地产物，失败不阻断结果上报。"""
        if self._artifact_uploader is None or not payload.artifact_upload_url:
            return []
        summary = result.summary or {}
        candidates: list[tuple[str, str, str | None]] = []
        report_path = summary.get("report_path")
        if isinstance(report_path, str) and report_path:
            candidates.append((report_path, "report", "pytest-junit.xml"))
        raw_paths = summary.get("artifact_paths") or []
        if isinstance(raw_paths, list):
            for item in raw_paths:
                if isinstance(item, str):
                    candidates.append((item, "data", None))
                elif isinstance(item, Mapping):
                    path = item.get("path")
                    if isinstance(path, str) and path:
                        candidates.append(
                            (
                                path,
                                str(item.get("kind") or "data"),
                                item.get("filename") if isinstance(item.get("filename"), str) else None,
                            )
                        )

        uploaded: list[dict] = []
        seen: set[str] = set()
        for raw_path, kind, filename in candidates:
            path = str(Path(raw_path).resolve())
            if path in seen:
                continue
            seen.add(path)
            try:
                reference = await self._artifact_uploader.upload(
                    payload.artifact_upload_url,
                    path,
                    kind=kind,
                    filename=filename,
                )
            except Exception as exc:  # noqa: BLE001 - 产物失败不阻断结果上报
                logger.warning(
                    "Run 产物上传失败: run_id=%s path=%s error=%s",
                    payload.run_id,
                    path,
                    exc,
                )
                continue
            uploaded.append(reference)
            logger.info(
                "Run 产物已上传: run_id=%s artifact_id=%s",
                payload.run_id,
                reference.get("artifact_id", ""),
            )
        return uploaded

    async def _finalize(
        self,
        payload: RunAssignPayload,
        plugin,
        context: TaskContext,
        result,
        artifact_refs: list[dict],
    ) -> None:
        await self._collect_logs(plugin, context)
        await self._flush_logs(context)
        await self._report_result(payload, plugin, context, result, artifact_refs)
        await self._report_log_complete(payload, context, artifact_refs)

    async def _collect_logs(self, plugin, context: TaskContext) -> None:
        """调用插件可选 collect_logs，整合插件内部日志（§9.5 规则 2）。"""
        collect = getattr(plugin, "collect_logs", None)
        if collect is None:
            return
        try:
            await collect(context)
        except Exception:
            logger.warning("插件 collect_logs 失败: run_id=%s", context.run_id, exc_info=True)

    async def _flush_logs(self, context: TaskContext) -> None:
        """把 spool 剩余日志按 RunLogBatch 分批上报并标记已发布。"""
        batch_size = self._settings.task_log_batch_size
        while True:
            entries = context.collect_pending_logs(batch_size)
            if not entries:
                return
            batch = context.build_log_batch(entries)
            if batch is None:
                return
            envelope = self._envelope(
                MessageType.RUN_LOG,
                context.run_id,
                batch.model_dump(mode="json"),
            )
            topic = event_topic(self._settings.node_id, "log")
            outbox_id = f"log:{context.run_id}:{batch.first_sequence}"
            self._ledger.replace_outbox(outbox_id, topic, envelope.model_dump(mode="json"))
            context.mark_logs_published([e.id for e in entries if e.id is not None])
            if len(entries) < batch_size:
                return

    async def _report_result(
        self,
        payload: RunAssignPayload,
        plugin,
        context: TaskContext,
        result,
        artifact_refs: list[dict],
    ) -> None:
        """组装并上报 run.result（稳定 outbox ID，一个 attempt 一个结果）。

        P6.5 语义：执行成功后调用 ``analyze_results`` 产出结构化 case 结果；
        分析入口抛异常时 result 标记 failed（§9.8：保留原始报告 artifact），
        不把未分析的结果误报为 succeeded（D-19）。
        """
        analysis = (
            await self._analyze(plugin, result, context)
            if result.status is AgentRunStatus.SUCCEEDED
            else _Analysis(failed=False)
        )

        if analysis.failed:
            # 分析失败：无论执行结果如何，最终 result 标记 failed
            status = "failed"
            passed = False
            case_results: list[CaseResultEntry] = []
            data = {"error": analysis.error or "result analysis failed"}
            metrics: dict = {}
        else:
            status = (
                "failed"
                if result.status is AgentRunStatus.SUCCEEDED and analysis.passed is False
                else _status_value(result.status)
            )
            case_results = analysis.case_results
            passed = bool(analysis.passed if analysis.passed is not None else result.status is AgentRunStatus.SUCCEEDED)
            metrics = analysis.metrics
            data = analysis.data
            if result.error and not data.get("error"):
                data = {**data, "error": result.error}

        run_result = RunResultPayload(
            run_id=payload.run_id,
            shard_id=payload.shard_id,
            attempt_no=payload.attempt_no,
            status=status,
            passed=passed,
            case_results=case_results,
            metrics=dict(metrics),
            data=dict(data),
            artifact_refs=list(artifact_refs),
        )
        envelope = self._envelope(
            MessageType.RUN_RESULT,
            payload.run_id,
            run_result.model_dump(mode="json"),
        )
        topic = event_topic(self._settings.node_id, "result")
        outbox_id = f"result:{payload.run_id}:{payload.attempt_no}"
        self._ledger.replace_outbox(outbox_id, topic, envelope.model_dump(mode="json"))
        logger.info(
            "run.result 已入 outbox: run_id=%s attempt=%s status=%s",
            payload.run_id,
            payload.attempt_no,
            status,
        )

    async def _analyze(self, plugin, result, context: TaskContext) -> _Analysis:
        """调用插件 analyze_results 产出结构化 case 结果。

        返回 ``_Analysis``：failed 表示分析入口异常（result 应标记 failed）；
        无 ``analyze_results`` 方法时视为成功但无结构化结果（passed 取执行态）。
        """
        analyze = getattr(plugin, "analyze_results", None)
        if analyze is None:
            return _Analysis(failed=False)

        try:
            analysis = await analyze(result.summary, context)
        except Exception as exc:
            logger.warning("插件 analyze_results 失败: run_id=%s", context.run_id, exc_info=True)
            return _Analysis(failed=True, error=f"{type(exc).__name__}: {exc}")

        if not isinstance(analysis, Mapping):
            return _Analysis(failed=True, error="analyze_results 必须返回 Mapping")
        try:
            return _Analysis.from_mapping(analysis)
        except Exception as exc:
            logger.warning("analyze_results 返回结构非法: run_id=%s", context.run_id, exc_info=True)
            return _Analysis(failed=True, error=f"非法结果结构: {exc}")

    async def _report_abort(self, payload: RunAssignPayload) -> None:
        """编排异常兜底：以 failed 结果上报，避免 Run 悬挂。"""
        run_result = RunResultPayload(
            run_id=payload.run_id,
            shard_id=payload.shard_id,
            attempt_no=payload.attempt_no,
            status="failed",
            passed=False,
            data={"error": "agent orchestration error"},
        )
        envelope = self._envelope(
            MessageType.RUN_RESULT,
            payload.run_id,
            run_result.model_dump(mode="json"),
        )
        topic = event_topic(self._settings.node_id, "result")
        outbox_id = f"result:{payload.run_id}:{payload.attempt_no}"
        self._ledger.replace_outbox(outbox_id, topic, envelope.model_dump(mode="json"))

    async def _report_log_complete(
        self,
        payload: RunAssignPayload,
        context: TaskContext,
        artifact_refs: list[dict],
    ) -> None:
        """发布 run.log-complete 日志围栏（P6.6）。

        记录当前 spool 已发布的最大 sequence 与总条数，写入稳定 outbox ID。
        发布在 result 之后，Master 收到后拒绝该 run 的任何日志条目。
        """
        max_sequence = self._ledger.get_published_log_stats(context.run_id)
        run_log_complete = RunLogCompletePayload(
            run_id=context.run_id,
            last_sequence=max_sequence["last_sequence"],
            entry_count=max_sequence["entry_count"],
            artifact_refs=list(artifact_refs),
        )
        envelope = self._envelope(
            MessageType.RUN_LOG_COMPLETE,
            context.run_id,
            run_log_complete.model_dump(mode="json"),
        )
        topic = event_topic(self._settings.node_id, "log-complete")
        outbox_id = f"log-complete:{context.run_id}"
        self._ledger.replace_outbox(outbox_id, topic, envelope.model_dump(mode="json"))
        logger.info(
            "run.log-complete 已入 outbox: run_id=%s last_sequence=%s count=%s",
            context.run_id,
            max_sequence["last_sequence"],
            max_sequence["entry_count"],
        )

    # -- 信封构造 -----------------------------------------------------------

    def _envelope(self, message_type: MessageType, trace_id: str, payload: dict) -> Envelope:
        return Envelope(
            message_id=uuid.uuid4().hex,
            message_type=message_type.value,
            sent_at=self._now(),
            sender=Sender(
                kind=SenderKind.AGENT,
                id=self._settings.node_id,
                session_id=self._session_id(),
            ),
            trace_id=trace_id,
            payload=payload,
        )
