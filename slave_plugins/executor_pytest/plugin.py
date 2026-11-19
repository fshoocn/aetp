"""pytest 执行端（从插件）入口：协议接线 / 幂等 / 取消 / 材料缓存（DESIGN §10）。

职责（§2.2）：

1. **能力声明**：``heartbeat/collect-capabilities`` 追加信封
   ``executor/pytest``（``detail.schema`` 逐字承载 §11.1 参数 JSON Schema，
   管理端任务下发 UI 动态生成表单）；``heartbeat/collect-status`` 扩展槽
   写入 ``stats()``（键 ``executor-pytest``）；
2. **接活流**（§10.1）：``test.job`` proto 校验（不认识拒收并回报 failed）→
   ``(job_id, attempt)`` 去重表（``data/plugins/executor_pytest/seen_jobs.json``，
   500 条 FIFO、**登记先于执行**）→ 材料就绪宽限（§5.2：``material_wait_s``
   内轮询缓存，期间发 ``test.job.progress {event: material-missing}`` 与
   ``pytest.material.missing``，超时回 ``failed: material missing``）→
   ``max_concurrent`` FIFO 队列执行；
3. **材料接收**（§5）：``pytest.material.chunk`` 自描述分块（不依赖元包先行，
   跨 topic 乱序安全）——按 ``(material_id, index)`` 幂等落盘，齐块拼装校验
   sha256，一致解压置 ready；不一致删分片回 ``missing`` 重拉一轮，再败放弃；
   ``pytest.material`` 元包缓存 recipe（组装依据，晚到无妨）+ selection 清单
   （§20-Q1 (a)：落 ``{cache}/{material_id}/selections/``）；
4. **inspect**（§6/§7）：``pytest.inspect`` 幂等 ``request_id``（应答缓存、
   重复请求重发同一应答），analyze 五项检查 / collect 结构化清单，
   应答 ``pytest.inspect.result``；
5. **上报**：``test.job.result``（AT_LEAST_ONCE）、``test.job.log`` /
   ``test.job.progress``（FIRE_AND_FORGET）；取消杀进程树、取消/超时仍上报
   已产生产物；组装工程目录在 result 上报后删、启动扫尾；
6. **缓存治理**（§3.2）：``material_retain_days`` + ``material_cache_max_bytes``
   双限，LRU 清非在跑 Job 占用的缓存。

族内私有 topic（``pytest.*``）不进契约：本包自管常量 + ``PROTO = 1``，
消息统一带 ``proto`` 字段，不认识版本拒收（D8）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Coroutine, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast

from cordis_port import Context, Service

from common.capabilities import CAP_EVENT, capability
from common.plugin_data import plugin_data_dir
from common.test_contracts import (
    TEST_PROTO,
    TOPIC_JOB,
    TOPIC_JOB_CANCEL,
    TOPIC_JOB_LOG,
    TOPIC_JOB_PROGRESS,
    TOPIC_JOB_RESULT,
    JobResult,
)

from .assemble import AssembleError, extract_zip, merge_recipe
from .config import PytestExecutorConfig
from .executor import JobRequest, RunResult, run_job, zero_counts
from .inspect import InspectMode, make_problem, run_inspect

# -- 族内私有 topic 与协议版本（不进契约，本包自管） -------------------------

#: 族内私有消息的协议版本（不认识版本拒收，D8）
PROTO = 1
#: 主→从：材料元包（recipe / selection 清单；chunk 不等它）
TOPIC_MATERIAL = "pytest.material"
#: 主→从：自描述材料分块（每条含 total / sha256，跨 topic 乱序安全）
TOPIC_MATERIAL_CHUNK = "pytest.material.chunk"
#: 从→主：缺块补发请求（missing = 坏 / 缺 index 清单）
TOPIC_MATERIAL_MISSING = "pytest.material.missing"
#: 主→从：脚本分析 / 用例解析请求（定向单节点）
TOPIC_INSPECT = "pytest.inspect"
#: 从→主：分析 / 解析应答
TOPIC_INSPECT_RESULT = "pytest.inspect.result"

#: ``heartbeat/collect-status`` 扩展收集槽（同步监听器写 ``extensions``）
STATUS_EVENT = "heartbeat/collect-status"
#: 扩展槽键（与插件 id 同名）
STATUS_KEY = "executor-pytest"

#: at-least-once：成功前重试（结果 / 材料 / inspect 应答）
_AT_LEAST_ONCE: dict[str, int] = {"min_success": 1}
#: fire-and-forget：发完即弃（日志流 / 进度，丢了无害）
_FIRE_AND_FORGET: dict[str, int] = {"min_success": 0, "max_attempts": 1}

#: 能力信封 detail（§11.1：shard 语义 + 参数 JSON Schema，逐字承载）
CAP_DETAIL: dict[str, object] = {
    "shard": "plan",
    "schema": {
        "type": "object",
        "properties": {
            "material_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "extra_args": {"type": "array", "items": {"type": "string"}},
            "keyword": {"type": "string"},
            "env": {"type": "object"},
            "recipe_override": {"type": "object"},
            "selection_id": {"type": "string"},
            "workdir": {"type": "string"}, "python": {"type": "string"},
        },
    },
}

#: 去重表容量（FIFO 裁剪）
_SEEN_LIMIT = 500
#: inspect 应答缓存容量（request_id 幂等）
_INSPECT_CACHE_LIMIT = 100
#: 材料宽限等待期间 missing 补发间隔（秒）
_MISSING_INTERVAL_S = 5.0
#: 材料就绪轮询间隔（秒）
_POLL_S = 0.5
#: inspect 临时目录名前缀（``{node}/workdir/.inspect/{request_id}``）
_INSPECT_DIR = ".inspect"

#: 路径段安全（job_id / request_id 落目录名用）
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")


def _safe_segment(value: str) -> str:
    """把外部 id 收敛成安全的单级目录名（防路径穿越）。"""
    return _SAFE_SEGMENT.sub("_", value)[:120] or "_"


# -- 内部队列项 --------------------------------------------------------------


@dataclass
class _QueuedJob:
    """一个已受理 Job（prepare → queue → worker 全程共用）。"""

    raw: dict[str, Any]
    job_id: str
    attempt: int
    task_id: str
    stage_name: str
    material_id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    canceled: bool = False
    accepted_ts: float = field(default_factory=time.time)


@dataclass(frozen=True)
class _RunningJob:
    """在跑 Job 的只读快照（running_jobs / 缓存占用判定）。"""

    job_id: str
    attempt: int
    task_id: str
    stage_name: str
    material_id: str
    started_ts: float


# -- (job_id, attempt) 去重表 ------------------------------------------------


class SeenJobs:
    """本地去重表（``seen_jobs.json``，500 条 FIFO）：只防重复消息（D5）。"""

    def __init__(self, path: Path, limit: int = _SEEN_LIMIT) -> None:
        self._path = path
        self._limit = limit
        self._entries: list[tuple[str, int]] = []
        self._load()

    def _load(self) -> None:
        try:
            raw: object = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        source = cast("Mapping[str, Any]", raw) if isinstance(raw, Mapping) else None
        rows = source.get("jobs") if source is not None else None
        if not isinstance(rows, list):
            return
        for raw_row in cast(list[object], rows):
            if not isinstance(raw_row, list):
                continue
            row = cast(list[object], raw_row)
            if len(row) == 2 and isinstance(row[0], str) and isinstance(row[1], int):
                self._entries.append((row[0], row[1]))

    def check_and_add(self, job_id: str, attempt: int) -> bool:
        """登记 ``(job_id, attempt)``；**已见过返回 False**（调用方丢弃）。"""
        key = (job_id, attempt)
        if key in self._entries:
            return False
        self._entries.append(key)
        if len(self._entries) > self._limit:
            del self._entries[: len(self._entries) - self._limit]
        self._save()
        return True

    def _save(self) -> None:
        payload = {"jobs": [[job_id, attempt] for job_id, attempt in self._entries]}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# -- 材料缓存（分块拼装 / ready / selection / GC，§3.2 / §5） -----------------


class MaterialStore:
    """从端材料缓存：``{cache}/{material_id}/`` = ready（解压内容 + selections）。

    目录布局::

        {cache}/{material_id}/            # ready：解压内容（含 selections/）
        {cache}/parts/{material_id}/      # 临时分片 {index:06d}.part
        {cache}/staging/{material_id}/    # 拼装产物 bundle.zip（组装来源）
        {cache}/meta/{material_id}.json   # 元包（recipe / selection 清单 / sha256）
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._parts_root = root / "parts"
        self._staging_root = root / "staging"
        self._meta_root = root / "meta"
        for directory in (self._root, self._parts_root, self._staging_root, self._meta_root):
            directory.mkdir(parents=True, exist_ok=True)
        #: sha 失败后的重拉轮数（一轮后放弃）
        self._resend_rounds: dict[str, int] = {}

    # -- 路径 ---------------------------------------------------------------
    def ready_dir(self, material_id: str) -> Path:
        return self._root / material_id

    def _parts_dir(self, material_id: str) -> Path:
        return self._parts_root / material_id

    def bundle_path(self, material_id: str) -> Path:
        return self._staging_root / material_id / "bundle.zip"

    def _meta_path(self, material_id: str) -> Path:
        return self._meta_root / f"{material_id}.json"

    # -- 状态 ---------------------------------------------------------------
    def is_ready(self, material_id: str) -> bool:
        """ready = 解压目录在 + 元包在（组装需要 recipe，§5.1）。"""
        return self.ready_dir(material_id).is_dir() and self._meta_path(material_id).is_file()

    def meta(self, material_id: str) -> dict[str, Any] | None:
        try:
            raw: object = json.loads(self._meta_path(material_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return dict(cast(Mapping[str, Any], raw)) if isinstance(raw, Mapping) else None

    def recipe_of(self, material_id: str) -> dict[str, Any] | None:
        meta = self.meta(material_id)
        if meta is None:
            return None
        recipe = meta.get("recipe")
        return dict(cast(Mapping[str, Any], recipe)) if isinstance(recipe, Mapping) else {}

    def total_of(self, material_id: str, fallback: int = 0) -> int:
        meta = self.meta(material_id)
        if meta is not None:
            try:
                return int(meta.get("total") or fallback)
            except (TypeError, ValueError):
                return fallback
        return fallback

    def missing(self, material_id: str, total: int) -> list[int]:
        """缺块 index 清单（total 未知返回空——主端按空清单整包补推）。"""
        if total <= 0:
            return []
        parts = self._parts_dir(material_id)
        return [index for index in range(total) if not (parts / f"{index:06d}.part").is_file()]

    # -- 元包（recipe / selections）-----------------------------------------
    def set_meta(
        self,
        material_id: str,
        *,
        sha256: str,
        recipe_fingerprint: str,
        total: int,
        chunk_size: int,
        recipe: Mapping[str, Any],
        selections: object,
    ) -> None:
        """缓存元包（晚到无妨）；ready 后立即物化 selection 清单。"""
        payload: dict[str, object] = {
            "material_id": material_id,
            "sha256": sha256,
            "recipe_fingerprint": recipe_fingerprint,
            "total": total,
            "chunk_size": chunk_size,
            "recipe": dict(recipe),
            "selections": cast(list[object], selections) if isinstance(selections, list) else [],
        }
        path = self._meta_path(material_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self._materialize_selections(material_id)

    def put_selection(self, material_id: str, selection_id: str, case_ids: list[str]) -> None:
        """落一份选择集（§20-Q1 (a)）：``{cache}/{material_id}/selections/{sid}.json``。"""
        directory = self.ready_dir(material_id) / "selections"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{selection_id}.json").write_text(
            json.dumps({"selection_id": selection_id, "case_ids": case_ids}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _materialize_selections(self, material_id: str) -> None:
        meta = self.meta(material_id)
        if meta is None or not self.ready_dir(material_id).is_dir():
            return
        rows = meta.get("selections")
        if not isinstance(rows, list):
            return
        for row in cast(list[object], rows):
            if not isinstance(row, Mapping):
                continue
            item = cast(Mapping[object, object], row)
            selection_id = item.get("selection_id")
            case_ids = item.get("case_ids")
            if isinstance(selection_id, str) and selection_id and isinstance(case_ids, list):
                self.put_selection(
                    material_id,
                    selection_id,
                    [str(case) for case in cast(list[object], case_ids) if isinstance(case, str)],
                )

    # -- 分块拼装 -----------------------------------------------------------
    def add_chunk(self, material_id: str, index: int, total: int, sha256: str, data: bytes) -> str:
        """幂等落一个分片；齐块拼装校验。

        返回：``stored``（落盘）/ ``duplicate``（已收跳过）/ ``assembled``
        （拼装解压完成）/ ``mismatch``（sha 不一致，删分片待重拉一轮）/
        ``gave-up``（重拉后再败，放弃并告警）。
        """
        if self.is_ready(material_id):
            return "duplicate"
        if index < 0 or total <= 0 or index >= total:
            return "mismatch"
        parts = self._parts_dir(material_id)
        parts.mkdir(parents=True, exist_ok=True)
        part = parts / f"{index:06d}.part"
        if part.is_file():
            return "duplicate"
        part.write_bytes(data)
        received = [path for path in parts.iterdir() if path.suffix == ".part"]
        if len(received) < total:
            return "stored"
        return self._assemble(material_id, total, sha256)

    def _assemble(self, material_id: str, total: int, sha256: str) -> str:
        """齐块后拼装：校验 sha256 → 解压置 ready；失败删分片重拉一轮。"""
        parts = self._parts_dir(material_id)
        bundle = self.bundle_path(material_id)
        bundle.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with bundle.open("wb") as output:
            for index in range(total):
                part = parts / f"{index:06d}.part"
                if not part.is_file():
                    self._discard_parts(material_id)
                    return self._after_mismatch(material_id)
                with part.open("rb") as source:
                    for block in iter(lambda: source.read(65536), b""):
                        digest.update(block)
                        output.write(block)
        if sha256 and digest.hexdigest() != sha256:
            bundle.unlink(missing_ok=True)
            self._discard_parts(material_id)
            return self._after_mismatch(material_id)
        ready = self.ready_dir(material_id)
        if ready.exists():
            shutil.rmtree(ready, ignore_errors=True)
        staging = self._root / f".staging-{_safe_segment(material_id)}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            extract_zip(bundle, staging)
            staging.rename(ready)
        except (AssembleError, OSError):
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(ready, ignore_errors=True)
            self._discard_parts(material_id)
            return self._after_mismatch(material_id)
        self._discard_parts(material_id)
        self._resend_rounds.pop(material_id, None)
        self._materialize_selections(material_id)
        self.touch(material_id)
        return "assembled"

    def _after_mismatch(self, material_id: str) -> str:
        rounds = self._resend_rounds.get(material_id, 0) + 1
        self._resend_rounds[material_id] = rounds
        return "mismatch" if rounds <= 1 else "gave-up"

    def _discard_parts(self, material_id: str) -> None:
        shutil.rmtree(self._parts_dir(material_id), ignore_errors=True)

    # -- 缓存治理（§3.2 双限 + LRU）------------------------------------------
    def touch(self, material_id: str) -> None:
        """LRU 触点：使用即刷新 mtime。"""
        now = time.time()
        for path in (self.ready_dir(material_id), self._meta_path(material_id)):
            try:
                os.utime(path, (now, now))
            except OSError:
                pass

    def gc(self, retain_days: float, max_bytes: int, in_use: set[str]) -> list[str]:
        """超期 + 超容量双限清理（LRU，跳过在跑 Job 占用）；返回被清 material_id。"""
        units = self._units()
        removed: list[str] = []
        now = time.time()
        for material_id, (paths, _size, mtime) in list(units.items()):
            if material_id in in_use:
                continue
            if retain_days <= 0 or (now - mtime) > retain_days * 86400:
                self._remove_unit(material_id, paths)
                removed.append(material_id)
                units.pop(material_id, None)
        total = sum(size for _, size, _ in units.values())
        if max_bytes > 0:
            for material_id, (paths, size, _) in sorted(
                units.items(), key=lambda item: item[1][2]
            ):
                if total <= max_bytes or material_id in in_use:
                    break
                self._remove_unit(material_id, paths)
                removed.append(material_id)
                total -= size
        return removed

    def _units(self) -> dict[str, tuple[list[Path], int, float]]:
        """缓存单元：ready + parts + staging + meta 合并计量。"""
        units: dict[str, tuple[list[Path], int, float]] = {}
        candidates: list[str] = []
        for path in self._root.iterdir():
            if path.name in ("parts", "staging", "meta") or path.name.startswith("."):
                continue
            if path.is_dir():
                candidates.append(path.name)
        for directory in (self._parts_root, self._staging_root, self._meta_root):
            for path in directory.iterdir():
                name = path.stem if directory is self._meta_root else path.name
                if name not in candidates:
                    candidates.append(name)
        for material_id in candidates:
            paths = [
                self.ready_dir(material_id),
                self._parts_dir(material_id),
                self.bundle_path(material_id).parent,
                self._meta_path(material_id),
            ]
            size = 0
            mtime = 0.0
            for path in paths:
                if path.is_file():
                    size += path.stat().st_size
                    mtime = max(mtime, path.stat().st_mtime)
                elif path.is_dir():
                    for sub in path.rglob("*"):
                        if sub.is_file():
                            size += sub.stat().st_size
                            mtime = max(mtime, sub.stat().st_mtime)
            units[material_id] = (paths, size, mtime)
        return units

    def _remove_unit(self, material_id: str, paths: list[Path]) -> None:
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)
        self._resend_rounds.pop(material_id, None)


# -- 插件主体 ----------------------------------------------------------------


class PytestExecutorPlugin(Service[PytestExecutorConfig]):
    """pytest 执行端插件（``provide = "executor_pytest"``，kind=slave）。

    ``inject`` 注入的是**服务**：``appconfig``（节点目录锚定）与 ``comm``
    （主从通道）。一切跨插件交互经 cordis：收发走 ``ctx.comm``，能力与
    状态扩展走事件收集槽。
    """

    name: str = "executor_pytest"
    provide: str | None = "executor_pytest"
    inject: ClassVar[list[str]] = ["appconfig", "comm"]
    Config: Any = PytestExecutorConfig

    def __init__(self, ctx: Context, config: PytestExecutorConfig | None = None) -> None:
        super().__init__(ctx)
        self.ctx: Context = ctx
        self.config: PytestExecutorConfig = (
            config if isinstance(config, PytestExecutorConfig) else PytestExecutorConfig()
        )
        loaded = ctx.appconfig.path_loaded
        self._node_dir: Path = (loaded.parent if loaded is not None else Path.cwd()).resolve()
        self._data_dir: Path = plugin_data_dir(self._node_dir, "executor_pytest")
        self._materials: MaterialStore = MaterialStore(self._data_dir / "materials")
        self._seen: SeenJobs = SeenJobs(self._data_dir / "seen_jobs.json")
        self._queue: asyncio.Queue[_QueuedJob] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._tasks: set[asyncio.Task[object]] = set()
        self._items: dict[tuple[str, int], _QueuedJob] = {}
        self._running: dict[tuple[str, int], _RunningJob] = {}
        self._inspect_cache: dict[str, dict[str, Any]] = {}
        #: 在途 inspect 的 request_id（幂等：在途重复不重跑，完成后补发同一应答）
        self._inspect_running: set[str] = set()
        self._inspect_resends: dict[str, int] = {}
        self._listeners: list[Any] = []
        self._stats: dict[str, int] = {
            "executed": 0,
            "canceled": 0,
            "timeout": 0,
            "deduped": 0,
            "proto_rejected": 0,
            "logs_sent": 0,
            "logs_dropped": 0,
            "results_reported": 0,
            "material_wait_total": 0,
        }

    # -- 生命周期 -----------------------------------------------------------
    async def init(self) -> object:
        """建目录、扫尾、注册收件与收集槽、启动 worker；返回卸载清理函数。"""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._sweep_workdirs()
        self._gc_cache()
        self.ctx.comm.on(TOPIC_JOB, self._on_job)
        self.ctx.comm.on(TOPIC_JOB_CANCEL, self._on_job_cancel)
        self.ctx.comm.on(TOPIC_MATERIAL, self._on_material)
        self.ctx.comm.on(TOPIC_MATERIAL_CHUNK, self._on_material_chunk)
        self.ctx.comm.on(TOPIC_INSPECT, self._on_inspect)
        self._listeners.append(self.ctx.on(CAP_EVENT, self._collect_capability))
        self._listeners.append(self.ctx.on(STATUS_EVENT, self._collect_status))
        for _ in range(max(1, self.config.max_concurrent)):
            task = asyncio.create_task(self._worker())
            self._workers.append(task)
        self.ctx.logger.info(
            "pytest 执行端已启动：node=%s 并发=%s 数据目录=%s",
            self._node_name(),
            self.config.max_concurrent,
            self._data_dir,
        )
        return self._stop

    async def _stop(self) -> None:
        """卸载清理：停 worker、取消在途任务、注销收集槽监听器。"""
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for dispose in self._listeners:
            result = dispose()
            if asyncio.iscoroutine(result):
                await result
        self._listeners.clear()

    def _spawn(self, coro: Coroutine[Any, Any, object]) -> None:
        """派生后台任务并登记（卸载统一取消）。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- 门面（§14）---------------------------------------------------------
    def stats(self) -> dict[str, int]:
        """运行统计（heartbeat/collect-status 扩展槽同源）。"""
        return {
            "queued": self._queue.qsize(),
            "running": len(self._running),
            **self._stats,
        }

    def running_jobs(self) -> list[dict[str, object]]:
        """在跑 Job 快照（按开始时间排序）。"""
        rows = sorted(self._running.values(), key=lambda item: item.started_ts)
        return [
            {
                "job_id": item.job_id,
                "attempt": item.attempt,
                "task_id": item.task_id,
                "stage_name": item.stage_name,
                "material_id": item.material_id,
                "started_ts": item.started_ts,
            }
            for item in rows
        ]

    # -- 收集槽监听器 -------------------------------------------------------
    def _collect_capability(self, bucket: object) -> None:
        """``heartbeat/collect-capabilities``：append 能力信封（§8.1 / §11.1）。"""
        if isinstance(bucket, list):
            cast(list[object], bucket).append(
                capability(
                    "executor",
                    "pytest",
                    "executor-pytest",
                    version="1.0.0",
                    labels={"lang": "python"},
                    summary="Python 单测执行（pytest）",
                    detail=CAP_DETAIL,
                )
            )

    def _collect_status(self, response: object) -> None:
        """``heartbeat/collect-status``：stats() 写扩展槽（键 executor-pytest）。"""
        if isinstance(response, dict):
            extensions = cast("dict[str, Any]", response).get("extensions")
            if isinstance(extensions, dict):
                cast("dict[str, Any]", extensions)[STATUS_KEY] = self.stats()

    # -- test.job：proto 校验 → 去重 → 材料宽限 → FIFO（§10.1）--------------
    def _on_job(self, payload: object, _meta: object) -> None:
        if not isinstance(payload, Mapping):
            return
        raw = {str(key): value for key, value in cast(Mapping[object, object], payload).items()}
        proto = _int_or(raw.get("proto"), 0)
        job_id = str(raw.get("job_id") or "")
        if proto != TEST_PROTO:
            self._stats["proto_rejected"] += 1
            if job_id:
                self._report_failure(job_id, self._attempt_of(raw), f"proto mismatch: got {proto}, support {TEST_PROTO}")
            self.ctx.logger.warning("job %s proto 不匹配（%s），拒收", job_id, proto)
            return
        executor = raw.get("executor")
        executor_id = (
            str(cast("Mapping[str, Any]", executor).get("id") or "")
            if isinstance(executor, Mapping)
            else ""
        )
        if executor_id and executor_id != "pytest":
            if job_id:
                self._report_failure(job_id, self._attempt_of(raw), f"executor mismatch: {executor_id}")
            self.ctx.logger.warning("job %s executor 不匹配（%s），拒收", job_id, executor_id)
            return
        attempt = self._attempt_of(raw)
        if not job_id or attempt <= 0:
            self._stats["proto_rejected"] += 1
            self.ctx.logger.warning("job 载荷缺 job_id / attempt，拒收")
            return
        if not self._seen.check_and_add(job_id, attempt):
            self._stats["deduped"] += 1
            self.ctx.logger.info("job %s a%s 重复消息，去重丢弃", job_id, attempt)
            return
        params = raw.get("params")
        typed_params: Mapping[str, Any] = cast("Mapping[str, Any]", params) if isinstance(params, Mapping) else {}
        item = _QueuedJob(
            raw=raw,
            job_id=job_id,
            attempt=attempt,
            task_id=str(raw.get("task_id") or ""),
            stage_name=str(raw.get("stage_name") or ""),
            material_id=str(typed_params.get("material_id") or ""),
        )
        self._items[(job_id, attempt)] = item
        self._spawn(self._prepare_job(item))

    def _attempt_of(self, raw: Mapping[str, Any]) -> int:
        try:
            return int(raw.get("attempt") or 0)
        except (TypeError, ValueError):
            return 0

    async def _prepare_job(self, item: _QueuedJob) -> None:
        """材料就绪宽限（§5.2）→ 入队；超时回 failed: material missing。"""
        try:
            if item.material_id:
                ready = await self._wait_material(item.material_id, item)
                if item.cancel_event.is_set():
                    self._finish_canceled(item)
                    return
                if not ready:
                    self._report_result(
                        item,
                        RunResult(
                            status="failed",
                            summary=zero_counts(),
                            cases=[],
                            artifacts=[],
                            error="material missing",
                            started_ts=item.accepted_ts,
                            finished_ts=time.time(),
                        ),
                    )
                    return
            await self._queue.put(item)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 —— 后台任务不许炸事件循环
            self.ctx.logger.error("job %s 准备失败：%s", item.job_id, error)
            self._report_result(
                item,
                RunResult(
                    status="failed",
                    summary=zero_counts(),
                    cases=[],
                    artifacts=[],
                    error=f"prepare error: {error}",
                    started_ts=item.accepted_ts,
                    finished_ts=time.time(),
                ),
            )

    async def _wait_material(self, material_id: str, item: _QueuedJob | None) -> bool:
        """宽限等待材料 ready；期间发 progress + ``pytest.material.missing``。"""
        if self._materials.is_ready(material_id):
            return True
        self._stats["material_wait_total"] += 1
        deadline = time.monotonic() + self.config.material_wait_s
        next_missing = 0.0
        while time.monotonic() < deadline:
            if item is not None and item.cancel_event.is_set():
                return False
            if self._materials.is_ready(material_id):
                return True
            now = time.monotonic()
            if now >= next_missing:
                self._send_material_missing(material_id, item)
                next_missing = now + _MISSING_INTERVAL_S
            await asyncio.sleep(_POLL_S)
        return self._materials.is_ready(material_id)

    def _send_material_missing(self, material_id: str, item: _QueuedJob | None) -> None:
        """兜底补推请求（§5.2）：progress 信号 + 缺块清单。"""
        total = self._materials.total_of(material_id)
        if item is not None:
            self._report_progress(item, event="material-missing", note=f"等待材料 {material_id}")
        try:
            self.ctx.comm.report(
                TOPIC_MATERIAL_MISSING,
                {
                    "proto": PROTO,
                    "material_id": material_id,
                    "node_id": self._node_name(),
                    "missing": self._materials.missing(material_id, total),
                },
                policy=dict(_AT_LEAST_ONCE),
            )
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("material.missing 发送失败：%s", error)

    # -- worker：FIFO 执行 --------------------------------------------------
    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item.canceled or item.cancel_event.is_set():
                    self._finish_canceled(item)
                else:
                    await self._execute(item)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                self.ctx.logger.error("job %s 执行异常：%s", item.job_id, error)
                self._report_result(
                    item,
                    RunResult(
                        status="failed",
                        summary=zero_counts(),
                        cases=[],
                        artifacts=[],
                        error=f"internal error: {error}",
                        started_ts=item.accepted_ts,
                        finished_ts=time.time(),
                    ),
                )
            finally:
                self._queue.task_done()

    async def _execute(self, item: _QueuedJob) -> None:
        """组装 + 执行 + 解析 + 上报 + 清理（§10.2-§10.3）。"""
        key = (item.job_id, item.attempt)
        started_ts = time.time()
        self._running[key] = _RunningJob(
            job_id=item.job_id,
            attempt=item.attempt,
            task_id=item.task_id,
            stage_name=item.stage_name,
            material_id=item.material_id,
            started_ts=started_ts,
        )
        self._report_progress(item, event="started")
        project_dir: Path | None = None
        try:
            request, project_dir = self._build_request(item)
            result = await run_job(
                request,
                cancel_event=item.cancel_event,
                send_chunk=self._send_log,
                log=self.ctx.logger,
            )
        except ValueError as error:
            result = RunResult(
                status="failed",
                summary=zero_counts(),
                cases=[],
                artifacts=[],
                error=str(error),
                started_ts=started_ts,
                finished_ts=time.time(),
            )
        self._report_result(item, result)
        if project_dir is not None:
            shutil.rmtree(project_dir, ignore_errors=True)
        self._running.pop(key, None)
        self._items.pop(key, None)
        self._stats["executed"] += 1
        if result.status == "canceled":
            self._stats["canceled"] += 1
        elif result.status == "timeout":
            self._stats["timeout"] += 1
        if item.material_id:
            self._materials.touch(item.material_id)
        self._gc_cache()

    def _build_request(self, item: _QueuedJob) -> tuple[JobRequest, Path]:
        """解析 params / 配方 / 路径（§11.2 / §15）→ JobRequest。"""
        raw = item.raw
        raw_params = raw.get("params")
        typed_params: Mapping[str, Any] = cast("Mapping[str, Any]", raw_params) if isinstance(raw_params, Mapping) else {}
        recipe_override = typed_params.get("recipe_override")
        base_recipe: Mapping[str, Any] = {}
        material_dir: Path | None = None
        zip_path: Path | None = None
        if item.material_id:
            material_dir = self._materials.ready_dir(item.material_id)
            recipe = self._materials.recipe_of(item.material_id)
            base_recipe = recipe if recipe is not None else {}
            zip_path = self._materials.bundle_path(item.material_id)
        override = cast(Mapping[str, Any], recipe_override) if isinstance(recipe_override, Mapping) else None
        recipe = merge_recipe(base_recipe, override)
        work_raw = typed_params.get("workdir") or self.config.workdir
        base = self._anchor_node(str(work_raw)) if work_raw else self._node_dir
        base = self._inside_node(base, "workdir")
        project_dir = self._inside_node(
            base / "workdir" / _safe_segment(item.job_id) / f"a{item.attempt}", "组装目录"
        )
        artifact_dir = self._inside_node(
            self._anchor_node(str(raw.get("artifact_dir") or "")), "artifact_dir"
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        shard = raw.get("shard") if isinstance(raw.get("shard"), Mapping) else None
        request = JobRequest(
            job_id=item.job_id,
            attempt=item.attempt,
            params=typed_params,
            shard=cast(Mapping[str, Any], shard) if shard is not None else None,
            recipe=recipe,
            config=self._sub_config(typed_params),
            zip_path=zip_path,
            project_dir=project_dir,
            artifact_dir=artifact_dir,
            material_dir=material_dir,
            deadline_ts=float(raw.get("deadline_ts") or 0.0),
        )
        return request, project_dir

    def _sub_config(self, params: Mapping[str, Any]) -> PytestExecutorConfig:
        """``params.python`` / ``params.workdir`` 覆盖执行端配置（§11.2）。"""
        override_python = params.get("python")
        if isinstance(override_python, str) and override_python.strip():
            values = self.config.to_dict()
            values["python"] = override_python.strip()
            return PytestExecutorConfig(**values)
        return self.config

    def _anchor_node(self, raw: str) -> Path:
        """节点本地路径解析（§8.4 / §15）：相对路径锚定**节点目录**，不是进程 cwd。"""
        path = Path(raw)
        return (path if path.is_absolute() else self._node_dir / path).resolve()

    def _inside_node(self, path: Path, label: str) -> Path:
        """路径安全（§15）：resolve 后必须在节点目录内。"""
        resolved = path.resolve()
        if not resolved.is_relative_to(self._node_dir):
            raise ValueError(f"{label} 越出节点目录：{path}")
        return resolved

    # -- test.job.cancel（§10.3）-------------------------------------------
    def _on_job_cancel(self, payload: object, _meta: object) -> None:
        if not isinstance(payload, Mapping):
            return
        raw = cast(Mapping[object, object], payload)
        job_id = str(raw.get("job_id") or "")
        attempt = self._attempt_of(cast(Mapping[str, Any], raw))
        if not job_id:
            return
        keys = [(job_id, attempt)] if attempt > 0 else [key for key in self._items if key[0] == job_id]
        hit = False
        for key in keys:
            item = self._items.get(key)
            if item is None:
                continue
            hit = True
            item.canceled = True
            item.cancel_event.set()
            self.ctx.logger.info("job %s a%s 收到取消：%s", job_id, key[1], raw.get("reason") or "")
        if not hit:
            self.ctx.logger.info("job %s 取消无在途项（已结束或未受理）", job_id)

    def _finish_canceled(self, item: _QueuedJob) -> None:
        """排队中 / 材料等待中被取消：立即回报 canceled（无产物）。"""
        self._report_result(
            item,
            RunResult(
                status="canceled",
                summary=zero_counts(),
                cases=[],
                artifacts=[],
                error="已取消",
                started_ts=item.accepted_ts,
                finished_ts=time.time(),
            ),
        )
        self._items.pop((item.job_id, item.attempt), None)
        self._stats["executed"] += 1
        self._stats["canceled"] += 1

    # -- 上报面 -------------------------------------------------------------
    def _report_result(self, item: _QueuedJob, result: RunResult) -> None:
        """``test.job.result``（AT_LEAST_ONCE，asdict(JobResult)）。"""
        payload = asdict(
            JobResult(
                proto=TEST_PROTO,
                job_id=item.job_id,
                attempt=item.attempt,
                status=result.status,
                summary=result.summary,
                cases=result.cases,
                artifacts=result.artifacts,
                started_ts=result.started_ts,
                finished_ts=result.finished_ts,
                error=result.error,
            )
        )
        try:
            self.ctx.comm.report(TOPIC_JOB_RESULT, payload, policy=dict(_AT_LEAST_ONCE))
            self._stats["results_reported"] += 1
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("job %s 结果上报失败：%s", item.job_id, error)

    def _report_failure(self, job_id: str, attempt: int, error: str) -> None:
        """拒收类失败回报（proto / executor 不匹配，§8.3）。"""
        item = _QueuedJob(
            raw={},
            job_id=job_id,
            attempt=max(attempt, 1),
            task_id="",
            stage_name="",
            material_id="",
        )
        self._report_result(
            item,
            RunResult(
                status="failed",
                summary=zero_counts(),
                cases=[],
                artifacts=[],
                error=error,
                started_ts=time.time(),
                finished_ts=time.time(),
            ),
        )

    def _report_progress(self, item: _QueuedJob, *, event: str, note: str = "") -> None:
        """``test.job.progress``（FIRE_AND_FORGET）。"""
        try:
            self.ctx.comm.report(
                TOPIC_JOB_PROGRESS,
                {"job_id": item.job_id, "attempt": item.attempt, "event": event, "note": note},
                policy=dict(_FIRE_AND_FORGET),
            )
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.warning("job %s 进度上报失败：%s", item.job_id, error)

    def _send_log(self, chunk: Mapping[str, Any]) -> None:
        """``test.job.log``（FIRE_AND_FORGET，asdict(LogChunk) 透传）。"""
        try:
            self.ctx.comm.report(TOPIC_JOB_LOG, dict(chunk), policy=dict(_FIRE_AND_FORGET))
            self._stats["logs_sent"] += 1
        except Exception as error:  # noqa: BLE001
            self._stats["logs_dropped"] += 1
            self.ctx.logger.warning("日志包发送失败：%s", error)

    # -- 材料接收（§5）-----------------------------------------------------
    def _on_material(self, payload: object, _meta: object) -> None:
        """元包：缓存 recipe（组装依据）+ selection 清单；晚到无妨。"""
        if not self._check_proto(payload, "pytest.material"):
            return
        raw = cast(Mapping[str, Any], payload)
        material_id = str(raw.get("material_id") or "")
        if not material_id:
            self.ctx.logger.warning("pytest.material 缺 material_id，丢弃")
            return
        recipe = raw.get("recipe")
        self._materials.set_meta(
            material_id,
            sha256=str(raw.get("sha256") or ""),
            recipe_fingerprint=str(raw.get("recipe_fingerprint") or ""),
            total=_int_or(raw.get("total"), 0),
            chunk_size=_int_or(raw.get("chunk_size"), 0),
            recipe=cast(Mapping[str, Any], recipe) if isinstance(recipe, Mapping) else {},
            selections=raw.get("selections"),
        )
        self.ctx.logger.info("材料 %s 元包已缓存", material_id)

    def _on_material_chunk(self, payload: object, _meta: object) -> None:
        """自描述分块：幂等落盘 → 齐块拼装校验 sha256 → 解压置 ready。"""
        if not self._check_proto(payload, "pytest.material.chunk"):
            return
        raw = cast(Mapping[str, Any], payload)
        material_id = str(raw.get("material_id") or "")
        index = _int_or(raw.get("index"), -1)
        total = _int_or(raw.get("total"), 0)
        if not material_id or index < 0 or total <= 0:
            self.ctx.logger.warning("pytest.material.chunk 载荷不完整，丢弃")
            return
        data_b64 = raw.get("data_b64")
        if not isinstance(data_b64, str):
            self.ctx.logger.warning("材料 %s 块 %s 缺 data_b64，丢弃", material_id, index)
            return
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError):
            self.ctx.logger.warning("材料 %s 块 %s base64 非法，丢弃", material_id, index)
            return
        outcome = self._materials.add_chunk(material_id, index, total, str(raw.get("sha256") or ""), data)
        if outcome == "assembled":
            self.ctx.logger.info("材料 %s 拼装就绪（%s 块）", material_id, total)
        elif outcome == "mismatch":
            self.ctx.logger.warning("材料 %s sha256 校验失败，删分片请求重拉", material_id)
            self._send_material_missing(material_id, None)
        elif outcome == "gave-up":
            self.ctx.logger.error("材料 %s 重拉后仍校验失败，放弃（请重新上传推送）", material_id)

    def _check_proto(self, payload: object, topic: str) -> bool:
        """族内消息 proto 校验（不认识版本拒收，D8）。"""
        if not isinstance(payload, Mapping):
            return False
        raw = cast(Mapping[str, Any], payload)
        proto = _int_or(raw.get("proto"), 0)
        if proto != PROTO:
            self._stats["proto_rejected"] += 1
            self.ctx.logger.warning("%s proto 不匹配（%s），拒收", topic, proto)
            return False
        return True

    # -- inspect（§6 / §7）-------------------------------------------------
    def _on_inspect(self, payload: object, _meta: object) -> None:
        """``pytest.inspect``：request_id 幂等（重复请求重发同一应答）。"""
        if not self._check_proto(payload, "pytest.inspect"):
            return
        raw = cast(Mapping[str, Any], payload)
        request_id = str(raw.get("request_id") or "")
        material_id = str(raw.get("material_id") or "")
        mode = str(raw.get("mode") or "")
        if not request_id or mode not in ("analyze", "collect"):
            self.ctx.logger.warning("pytest.inspect 载荷不合法（request_id=%s mode=%s），丢弃", request_id, mode)
            return
        cached = self._inspect_cache.get(request_id)
        if cached is not None:
            self._report_inspect(cached)
            return
        if request_id in self._inspect_running:
            # 在途重复（§6.1 幂等）：不重跑，完成后把同一应答补发给每个重复请求
            self._inspect_resends[request_id] = self._inspect_resends.get(request_id, 0) + 1
            return
        self._inspect_running.add(request_id)
        self._spawn(self._do_inspect(request_id, material_id, mode))

    async def _do_inspect(self, request_id: str, material_id: str, mode: InspectMode) -> None:
        """临时目录组装后检查 / 解析；应答缓存 + 上报。"""
        response: dict[str, Any]
        started = time.time()
        try:
            if not material_id or not await self._wait_material(material_id, None):
                response = self._inspect_response(
                    request_id,
                    material_id,
                    mode,
                    False,
                    [make_problem("material.missing", f"材料未就绪：{material_id}")],
                    [],
                    started,
                )
            else:
                recipe = self._materials.recipe_of(material_id) or {}
                temp_dir = self._node_dir / "workdir" / _INSPECT_DIR / _safe_segment(request_id)
                outcome = await run_inspect(
                    mode,
                    zip_path=self._materials.bundle_path(material_id),
                    recipe=recipe,
                    temp_dir=temp_dir,
                    config=self.config,
                )
                response = self._inspect_response(
                    request_id,
                    material_id,
                    mode,
                    bool(outcome.get("ok")),
                    cast(list[dict[str, Any]], outcome.get("problems") or []),
                    cast(list[dict[str, Any]], outcome.get("cases") or []),
                    started,
                )
        except Exception as error:  # noqa: BLE001
            response = self._inspect_response(
                request_id,
                material_id,
                mode,
                False,
                [make_problem("inspect.internal", f"inspect 执行异常：{error}")],
                [],
                started,
            )
        self._inspect_cache[request_id] = response
        while len(self._inspect_cache) > _INSPECT_CACHE_LIMIT:
            self._inspect_cache.pop(next(iter(self._inspect_cache)))
        self._inspect_running.discard(request_id)
        self._report_inspect(response)
        for _ in range(self._inspect_resends.pop(request_id, 0)):
            self._report_inspect(response)

    def _inspect_response(
        self,
        request_id: str,
        material_id: str,
        mode: InspectMode,
        ok: bool,
        problems: list[dict[str, Any]],
        cases: list[dict[str, Any]],
        started: float,
    ) -> dict[str, Any]:
        return {
            "proto": PROTO,
            "request_id": request_id,
            "material_id": material_id,
            "mode": mode,
            "node_id": self._node_name(),
            "ok": ok,
            "problems": problems,
            "cases": cases,
            "duration_s": round(time.time() - started, 3),
        }

    def _report_inspect(self, response: Mapping[str, Any]) -> None:
        """``pytest.inspect.result``（AT_LEAST_ONCE）。"""
        try:
            self.ctx.comm.report(TOPIC_INSPECT_RESULT, dict(response), policy=dict(_AT_LEAST_ONCE))
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error("inspect 应答发送失败：%s", error)

    # -- 维护 ---------------------------------------------------------------
    def _node_name(self) -> str:
        value = self.ctx.get("node_name", strict=False)
        return str(value) if value else ""

    def _sweep_workdirs(self) -> None:
        """启动扫尾（§3.2）：清崩溃残留的组装目录 / inspect 临时目录。"""
        work_root = self._node_dir / "workdir"
        if not work_root.is_dir():
            return
        for path in work_root.iterdir():
            shutil.rmtree(path, ignore_errors=True)

    def _gc_cache(self) -> None:
        """缓存双限治理（§3.2）：LRU 清非在跑 Job 占用的材料缓存。"""
        in_use = {item.material_id for item in self._running.values() if item.material_id}
        removed = self._materials.gc(
            self.config.material_retain_days,
            self.config.material_cache_max_bytes,
            in_use,
        )
        if removed:
            self.ctx.logger.info("材料缓存清理：%s", ", ".join(removed))


def _int_or(raw: object, fallback: int) -> int:
    """宽松取 int（bool 拒绝）：int/float/字符串数字收敛，其余回退。"""
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return fallback
    return fallback


__all__: list[str] = ["PROTO", "PytestExecutorPlugin"]
