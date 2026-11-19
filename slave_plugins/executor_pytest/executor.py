"""执行内核（DESIGN §10.3）：argv 组装 / 子进程执行 / junit 解析 → JobResult 部件。

* **argv 组装**：``[python, -m, pytest, *targets, *参数, --junitxml, …, -p aetp_marker]``
  （决策 D7：argv 化、``shell=False``，拒命令注入）；targets 优先级——
  分片 ``unit_refs`` → ``params.targets`` → ``params.selection_id`` 展开
  （§20-Q1 定案 (a)：selection 清单随材料预推落
  ``{cache}/{material_id}/selections/{selection_id}.json``，执行端本地展开）→
  ``recipe.entry`` 兜底；
* **执行**：``asyncio.create_subprocess_exec``；env = ``os.environ`` 增量 +
  ``recipe.env`` + ``params.env``（**拒 ``AETP_*`` 保留前缀**，§15 纵深）+
  ``PYTHONIOENCODING=utf-8`` / ``PYTHONUNBUFFERED=1``；超时 =
  ``min(deadline_ts 余量, config.timeout_s)``；取消优先于超时；杀**进程树**
  （D3：Windows ``taskkill /T /F``、POSIX 进程组 ``SIGKILL``）；
* **junit 解析**：stdlib ``xml.etree``，六种 status（passed / failed / skipped /
  error / xfailed / xpassed），message 截断 2 KiB；case_id 优先 marker 记录的
  nodeid（哨兵顺序对齐）、回退 ``classname::name``；
* 取消 / 超时仍上报已产生产物；产物 ``junit.xml``(report) / ``pytest.log``(log)，
  sha256 / size 自算，``uri = local:///`` + 绝对路径 ``as_posix`` 盘符形态。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import signal
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from common.test_contracts import ArtifactRef, CaseResult

from .assemble import AssembleError, apply_layout, extract_zip
from .config import PytestExecutorConfig
from .stream import LogStreamer

#: CaseResult 六种 status（契约 CaseResult.status 同形）
CaseStatus = Literal["passed", "failed", "skipped", "error", "xfailed", "xpassed"]
#: JobResult 四种终态（契约 JobResult.status 同形）
JobStatus = Literal["succeeded", "failed", "canceled", "timeout"]


class LineSink(Protocol):
    """子进程逐行输出的消费面（LogStreamer / 检查探针的捕获器都实现它）。"""

    def feed(self, stream: Literal["stdout", "stderr"], raw_line: str) -> None:
        """喂一行原始输出（不含换行符）。"""
        ...

#: 失败 / 错误摘要的截断上限（2 KiB，§10.3）
_MESSAGE_MAX = 2048
#: 子进程结束判定的尾批等待上限（秒）
_DRAIN_TIMEOUT_S = 5.0

#: marker 插件目录（随包分发；经 PYTHONPATH 注入子进程，``-p aetp_marker`` 启用）
MARKER_DIR: Path = Path(__file__).resolve().parent / "marker_plugin"


class JobRunError(Exception):
    """执行前的基础设施级错误（selection 缺失、产物目录越界等）。"""


@dataclass(frozen=True)
class ProcessOutcome:
    """一次子进程执行的终态。"""

    exit_code: int | None
    timed_out: bool
    canceled: bool


@dataclass(frozen=True)
class RunResult:
    """执行结果部件（插件层补 proto / job_id / attempt 后上报）。"""

    status: JobStatus
    summary: dict[str, Any]
    cases: list[CaseResult]
    artifacts: list[ArtifactRef]
    error: str = ""
    started_ts: float = 0.0
    finished_ts: float = 0.0
    #: plan 型 job 的分片规划（summary.shards 同源；仅 plan job 非空）
    shards: list[dict[str, Any]] | None = None


def local_uri(path: Path) -> str:
    """产物 URI：``local:///`` + 绝对路径 ``as_posix`` 盘符形态（local:///D:/…）。"""
    return "local:///" + path.resolve().as_posix()


def file_digest(path: Path) -> tuple[str, int]:
    """文件 sha256 与字节数。"""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


# -- 环境与 argv 组装 --------------------------------------------------------


def build_env(
    base: Mapping[str, str],
    recipe_env: object,
    params_env: object,
    *,
    marker_dir: Path = MARKER_DIR,
) -> tuple[dict[str, str], list[str]]:
    """组装子进程环境；``AETP_*`` 保留前缀的键拒绝应用（返回被拒清单）。

    顺序：``os.environ`` 增量 → ``recipe.env`` → ``params.env``；``marker_dir``
    前插 ``PYTHONPATH``（``-p aetp_marker`` 的导入来源）；补
    ``PYTHONIOENCODING=utf-8`` / ``PYTHONUNBUFFERED=1``。
    """
    env = dict(base)
    dropped: list[str] = []
    for source in (recipe_env, params_env):
        if not isinstance(source, Mapping):
            continue
        for raw_key, raw_value in cast(Mapping[object, object], source).items():
            key = str(raw_key)
            if key.startswith("AETP_"):
                dropped.append(key)
                continue
            env[key] = str(raw_value)
    python_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{marker_dir}{os.pathsep}{python_path}" if python_path else str(marker_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env, dropped


def resolve_targets(
    params: Mapping[str, Any],
    recipe: Mapping[str, Any],
    shard: Mapping[str, Any] | None,
    material_dir: Path | None,
) -> list[str]:
    """执行目标优先级（§10.3）：分片 unit_refs → targets → selection 展开 → entry。"""
    if shard is not None and not shard.get("plan"):
        unit_refs = shard.get("unit_refs")
        if isinstance(unit_refs, (list, tuple)) and unit_refs:
            return [str(item) for item in cast(Sequence[object], unit_refs)]
    targets = params.get("targets")
    if isinstance(targets, (list, tuple)) and targets:
        return [str(item) for item in cast(Sequence[object], targets)]
    selection_id = params.get("selection_id")
    if isinstance(selection_id, str) and selection_id:
        return expand_selection(selection_id, material_dir)
    entry = recipe.get("entry")
    if isinstance(entry, str) and entry.strip():
        return [entry.strip()]
    raise JobRunError("没有可执行目标：unit_refs / targets / selection_id / recipe.entry 均为空")


def expand_selection(selection_id: str, material_dir: Path | None) -> list[str]:
    """展开选择集（§20-Q1 (a)）：读 ``{cache}/{material_id}/selections/{sid}.json``。"""
    if material_dir is None:
        raise JobRunError(f"选择集 {selection_id!r} 无法展开：材料缓存缺失")
    selection_path = material_dir / "selections" / f"{selection_id}.json"
    if not selection_path.is_file():
        raise JobRunError(f"选择集不存在：{selection_id}")
    try:
        raw: object = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JobRunError(f"选择集解析失败：{selection_id}（{exc}）") from exc
    case_ids: object
    if isinstance(raw, Mapping):
        case_ids = cast("Mapping[str, Any]", raw).get("case_ids")
    else:
        case_ids = raw
    if not isinstance(case_ids, (list, tuple)) or not all(
        isinstance(item, str) for item in cast(Sequence[object], case_ids)
    ):
        raise JobRunError(f"选择集格式非法（需 case_ids 字符串数组）：{selection_id}")
    values = [item for item in cast(Sequence[str], case_ids) if item]
    if not values:
        raise JobRunError(f"选择集为空：{selection_id}")
    return values


def build_argv(
    python: str,
    targets: Sequence[str],
    params: Mapping[str, Any],
    recipe: Mapping[str, Any],
    config: PytestExecutorConfig,
    junit_path: Path,
) -> list[str]:
    """pytest argv（§10.3）：targets + 默认参数 + 配方参数 + 任务参数 + junit/marker。"""
    argv = [python, "-m", "pytest", *targets]
    argv.extend(shlex.split(config.pytest_args))
    recipe_args = recipe.get("args")
    if isinstance(recipe_args, list):
        argv.extend(str(item) for item in cast(list[object], recipe_args))
    extra_args = params.get("extra_args")
    if isinstance(extra_args, (list, tuple)) and extra_args:
        argv.extend(str(item) for item in cast(Sequence[object], extra_args))
    else:
        keyword = params.get("keyword")
        if isinstance(keyword, str) and keyword.strip():
            argv.extend(["-k", keyword.strip()])
    argv.extend(["--junitxml", str(junit_path), "-p", "aetp_marker"])
    return argv


# -- 子进程执行（杀进程树：D3） ----------------------------------------------


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    streamer: LineSink | None,
    cancel_event: asyncio.Event | None = None,
) -> ProcessOutcome:
    """跑一个子进程：双流捕获 → streamer；超时 / 取消杀进程树后收尾。"""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name != "nt"),
    )
    readers = [
        asyncio.create_task(_pump(process.stdout, "stdout", streamer)),
        asyncio.create_task(_pump(process.stderr, "stderr", streamer)),
    ]
    waiter = asyncio.create_task(process.wait())
    watchers: set[asyncio.Task[object]] = {waiter}
    cancel_watcher: asyncio.Task[object] | None = None
    if cancel_event is not None:
        cancel_watcher = asyncio.create_task(cancel_event.wait())
        watchers.add(cancel_watcher)
    timed_out = False
    canceled = False
    done, pending = await asyncio.wait(watchers, timeout=max(0.05, timeout_s), return_when=asyncio.FIRST_COMPLETED)
    if not done:
        timed_out = True
        await _kill_tree(process)
        await waiter
    elif cancel_watcher is not None and cancel_watcher in done and not waiter.done():
        canceled = True
        await _kill_tree(process)
        await waiter
    for task in pending:
        task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), _DRAIN_TIMEOUT_S)
    except TimeoutError:
        for task in readers:
            task.cancel()
    _close_pipes(process)
    return ProcessOutcome(exit_code=process.returncode, timed_out=timed_out, canceled=canceled)


def _close_pipes(process: asyncio.subprocess.Process) -> None:
    """收尾关闭子进程管道传输（Windows proactor 的未关闭句柄 / ``__del__`` 告警）。"""
    for stream in (process.stdout, process.stderr):
        transport = getattr(stream, "_transport", None)
        close = getattr(transport, "close", None)
        if callable(close):
            close()


async def _pump(
    reader: asyncio.StreamReader | None,
    stream: Literal["stdout", "stderr"],
    streamer: LineSink | None,
) -> None:
    """逐行读子进程输出喂给 streamer（utf-8 + replace 容错）。"""
    if reader is None:
        return
    while True:
        raw = await reader.readline()
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace")
        if streamer is not None:
            streamer.feed(stream, line)


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """杀进程树（D3）：Windows taskkill /T /F，POSIX 进程组 SIGKILL；兜底 kill。"""
    pid = process.pid
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=10.0)
        except (OSError, TimeoutError):
            process.kill()
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()


# -- junit 解析 --------------------------------------------------------------


def parse_junit(junit_path: Path, nodeids: Sequence[str]) -> tuple[list[CaseResult], dict[str, int]]:
    """解析 junit.xml → ``(CaseResult 列表, 计数摘要)``（六种 status）。

    XML 口径（pytest 8.x 实测）：``<skipped type="pytest.xfail">`` → xfailed；
    ``<failure message="[XPASS(strict)] …">`` → xpassed；其余 ``skipped`` /
    ``failure`` / ``error`` 各归各；裸 testcase → passed（非严格 xpass 在
    junit 里与 passed 同形，如实归 passed）。case_id 与 marker 记录的 nodeid
    按出现顺序对齐，对不齐回退 ``classname::name``。
    """
    try:
        tree = ET.parse(junit_path)  # noqa: S314 —— junit.xml 产自本插件的 pytest 子进程（D16 内部可信）
    except (ET.ParseError, OSError):
        return [], zero_counts()
    cases: list[CaseResult] = []
    counts = zero_counts()
    for index, element in enumerate(tree.iter("testcase")):
        name = str(element.get("name") or "")
        classname = str(element.get("classname") or "")
        duration = _float_attr(element.get("time"))
        nodeid = nodeids[index] if index < len(nodeids) else ""
        case_id = nodeid or f"{classname}::{name}"
        status, message = _classify(element)
        cases.append(
            CaseResult(
                case_id=case_id,
                status=status,
                duration_s=duration,
                message=_truncate_message(message),
            )
        )
        counts["total"] += 1
        counts["errors" if status == "error" else status] += 1
    return cases, counts


def zero_counts() -> dict[str, int]:
    """summary 计数摘要的零值骨架（§10.3 键全）。"""
    return {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
    }


def _classify(element: ET.Element) -> tuple[CaseStatus, str]:
    """单个 testcase 的 status 与摘要（按 junit 子元素判定）。"""
    failure = element.find("failure")
    error = element.find("error")
    skipped = element.find("skipped")
    if error is not None:
        return "error", error.get("message") or (error.text or "")
    if failure is not None:
        message = failure.get("message") or (failure.text or "")
        if message.startswith("[XPASS(strict)]"):
            return "xpassed", message
        return "failed", message
    if skipped is not None:
        message = skipped.get("message") or (skipped.text or "")
        if skipped.get("type") == "pytest.xfail":
            return "xfailed", message
        return "skipped", message
    return "passed", ""


def _float_attr(raw: str | None) -> float:
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def _truncate_message(message: str) -> str:
    """失败摘要截断 2 KiB（UTF-8 字节口径，不劈多字节字符）。"""
    encoded = message.encode("utf-8", errors="replace")
    if len(encoded) <= _MESSAGE_MAX:
        return message
    return encoded[:_MESSAGE_MAX].decode("utf-8", errors="ignore")


# -- plan 分片规划（§9.3） ---------------------------------------------------


def plan_shards(case_ids: Sequence[str], files_per_shard: int) -> list[dict[str, Any]]:
    """按文件级（D1）把用例文件分组：每片至多 ``files_per_shard`` 个文件。"""
    files: list[str] = []
    for case_id in case_ids:
        path = case_id.split("::", 1)[0]
        if path and path not in files:
            files.append(path)
    size = max(1, int(files_per_shard))
    groups = [files[index : index + size] for index in range(0, len(files), size)]
    total = len(groups)
    return [
        {"index": index, "total": total, "unit_refs": list(group)}
        for index, group in enumerate(groups)
    ]


# -- 执行主流程 --------------------------------------------------------------


@dataclass(frozen=True)
class JobRequest:
    """一次执行所需上下文（插件层组装）。"""

    job_id: str
    attempt: int
    params: Mapping[str, Any]
    shard: Mapping[str, Any] | None
    recipe: Mapping[str, Any]
    config: PytestExecutorConfig
    #: 材料 bundle.zip（组装来源）；无材料任务为 None（跳过组装）
    zip_path: Path | None
    #: 组装目标：``{workdir}/workdir/{job_id}/a{attempt}``
    project_dir: Path
    #: 产物目录（核心生成、执行器只写不删）
    artifact_dir: Path
    #: 材料缓存目录（selection 展开用）；无材料任务为 None
    material_dir: Path | None
    deadline_ts: float


async def run_job(
    request: JobRequest,
    *,
    cancel_event: asyncio.Event,
    send_chunk: Callable[[Mapping[str, Any]], None],
    log: Any,
) -> RunResult:
    """组装 + 执行 + 解析 → :class:`RunResult`（含取消 / 超时路径）。"""
    started_ts = time.time()
    try:
        if request.zip_path is not None:
            extract_zip(request.zip_path, request.project_dir)
            apply_layout(request.project_dir, request.recipe)
        else:
            request.project_dir.mkdir(parents=True, exist_ok=True)
    except AssembleError as error:
        log.warning("job %s 组装失败：%s", request.job_id, error)
        return _bare_result("failed", f"assemble error: {error}", started_ts)

    junit_path = request.artifact_dir / "junit.xml"
    log_path = request.artifact_dir / "pytest.log"
    request.artifact_dir.mkdir(parents=True, exist_ok=True)
    streamer = LogStreamer(
        job_id=request.job_id,
        attempt=request.attempt,
        log_path=log_path,
        send_chunk=send_chunk,
        batch_lines=request.config.log_batch_lines,
        batch_interval=request.config.log_batch_interval,
        rate_limit=request.config.log_rate_limit,
        line_max_bytes=request.config.line_max_bytes,
    )
    flush_task = asyncio.create_task(streamer.run())
    result: RunResult
    try:
        python = request.config.python or sys.executable
        if request.shard is not None and request.shard.get("plan"):
            result = await _run_plan(request, python, streamer, cancel_event, started_ts, log)
        else:
            targets = resolve_targets(request.params, request.recipe, request.shard, request.material_dir)
            argv = build_argv(python, targets, request.params, request.recipe, request.config, junit_path)
            env, dropped = build_env(
                os.environ,
                request.recipe.get("env"),
                request.params.get("env"),
            )
            for key in dropped:
                log.warning("job %s 拒绝 AETP_* 环境变量：%s", request.job_id, key)
            timeout_s = resolve_timeout(request.deadline_ts, request.config.timeout_s)
            if timeout_s <= 0:
                result = _bare_result("timeout", "deadline 已过期", started_ts)
            else:
                outcome = await run_process(
                    argv,
                    cwd=request.project_dir,
                    env=env,
                    timeout_s=timeout_s,
                    streamer=streamer,
                    cancel_event=cancel_event,
                )
                nodeids = list(streamer.case_ids_seen)
                cases, counts = parse_junit(junit_path, nodeids)
                result = _finish_result(outcome, cases, counts, started_ts)
    except JobRunError as error:
        result = _bare_result("failed", str(error), started_ts)
    finally:
        await _close_streamer(streamer, flush_task)
    # 产物在日志流收尾后统一计量（sha256/size 覆盖完整件）；取消/超时仍上报
    return replace(result, artifacts=_collect_artifacts(request))


async def _run_plan(
    request: JobRequest,
    python: str,
    streamer: LogStreamer,
    cancel_event: asyncio.Event,
    started_ts: float,
    log: Any,
) -> RunResult:
    """plan 型 job：只收集不执行，产出 ``summary.shards``（§9.3 文件级分片）。"""
    collect_out = request.artifact_dir / "collect.json"
    argv = [python, "-m", "pytest", "--collect-only", "-q", "-p", "aetp_marker"]
    env, dropped = build_env(
        os.environ,
        request.recipe.get("env"),
        request.params.get("env"),
    )
    env["AETP_MARKER_OUT"] = str(collect_out)
    for key in dropped:
        log.warning("job %s 拒绝 AETP_* 环境变量：%s", request.job_id, key)
    timeout_s = resolve_timeout(request.deadline_ts, request.config.timeout_s)
    if timeout_s <= 0:
        return _bare_result("timeout", "deadline 已过期", started_ts)
    outcome = await run_process(
        argv,
        cwd=request.project_dir,
        env=env,
        timeout_s=timeout_s,
        streamer=streamer,
        cancel_event=cancel_event,
    )
    if outcome.canceled:
        return _bare_result("canceled", "已取消", started_ts)
    if outcome.timed_out:
        return _bare_result("timeout", "执行超时", started_ts)
    case_ids = _read_collected_ids(collect_out)
    if outcome.exit_code != 0:
        return _bare_result("failed", f"collect 失败：pytest 退出码 {outcome.exit_code}", started_ts)
    shards: list[dict[str, Any]] = plan_shards(case_ids, request.config.shard_files)
    if not shards:
        shards = [{"index": 0, "total": 1, "unit_refs": []}]
    summary: dict[str, Any] = {"total": len(case_ids), "shards": shards}
    finished_ts = time.time()
    return RunResult(
        status="succeeded",
        summary=summary,
        cases=[],
        artifacts=[],
        error="",
        started_ts=started_ts,
        finished_ts=finished_ts,
        shards=shards,
    )


def _read_collected_ids(collect_out: Path) -> list[str]:
    """读 marker 插件导出的用例清单（collect.json）里的 nodeid。"""
    try:
        raw: object = json.loads(collect_out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    source = cast("Mapping[str, Any]", raw) if isinstance(raw, Mapping) else None
    cases = source.get("cases") if source is not None else None
    if not isinstance(cases, list):
        return []
    ids: list[str] = []
    for item in cast(list[object], cases):
        if isinstance(item, Mapping):
            case_id = cast("Mapping[str, Any]", item).get("case_id")
            if isinstance(case_id, str) and case_id:
                ids.append(case_id)
    return ids


def resolve_timeout(deadline_ts: float, timeout_s: float) -> float:
    """执行超时 = ``min(deadline_ts 余量, config.timeout_s)``（deadline 0 = 不限）。"""
    if deadline_ts and deadline_ts > 0:
        return min(deadline_ts - time.time(), timeout_s)
    return timeout_s


def _finish_result(
    outcome: ProcessOutcome,
    cases: list[CaseResult],
    counts: dict[str, int],
    started_ts: float,
) -> RunResult:
    """子进程终态 → RunResult（succeeded ⟺ failed+errors==0 且非取消/超时）。"""
    finished_ts = time.time()
    if outcome.canceled:
        return RunResult("canceled", counts, cases, [], "已取消", started_ts, finished_ts)
    if outcome.timed_out:
        return RunResult("timeout", counts, cases, [], "执行超时", started_ts, finished_ts)
    failed = int(counts.get("failed", 0)) + int(counts.get("errors", 0))
    error = ""
    status: JobStatus = "succeeded"
    if outcome.exit_code not in (0, 1):
        status = "failed"
        error = f"pytest 退出码 {outcome.exit_code}"
    elif failed > 0:
        status = "failed"
    return RunResult(status, counts, cases, [], error, started_ts, finished_ts)


def _bare_result(status: JobStatus, error: str, started_ts: float) -> RunResult:
    """无产物的裸结果（组装失败 / 提前失败 / 取消 / 超时）。"""
    return RunResult(
        status=status,
        summary=zero_counts(),
        cases=[],
        artifacts=[],
        error=error,
        started_ts=started_ts,
        finished_ts=time.time(),
    )


def _collect_artifacts(request: JobRequest) -> list[ArtifactRef]:
    """产物清单：junit.xml(report) + pytest.log(log)，sha256 / size 自算。"""
    entries: list[tuple[str, Literal["log", "report", "attachment"], str]] = [
        ("junit.xml", "report", "report"),
        ("pytest.log", "log", "log"),
    ]
    artifacts: list[ArtifactRef] = []
    for name, kind, suffix in entries:
        path = request.artifact_dir / name
        if not path.is_file():
            continue
        digest, size = file_digest(path)
        artifacts.append(
            ArtifactRef(
                artifact_id=f"{request.job_id}-a{request.attempt}-{suffix}",
                kind=kind,
                name=name,
                uri=local_uri(path),
                size=size,
                sha256=digest,
            )
        )
    return artifacts


async def _close_streamer(streamer: LogStreamer, flush_task: asyncio.Task[None]) -> None:
    """关日志流：冲掉尾批并停批量循环。"""
    await streamer.close()
    flush_task.cancel()
    try:
        await flush_task
    except (asyncio.CancelledError, TimeoutError):
        pass


__all__: list[str] = [
    "MARKER_DIR",
    "CaseStatus",
    "JobRequest",
    "JobRunError",
    "JobStatus",
    "LineSink",
    "ProcessOutcome",
    "RunResult",
    "build_argv",
    "build_env",
    "expand_selection",
    "file_digest",
    "local_uri",
    "parse_junit",
    "plan_shards",
    "resolve_targets",
    "resolve_timeout",
    "run_job",
    "run_process",
    "zero_counts",
]
