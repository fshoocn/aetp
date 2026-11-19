"""executor_pytest 管理端插件（``provide = "executor_pytest_master"``，kind=master）。

职责（executor_pytest DESIGN §2/§5/§6）：

1. **材料预推 / 缺块补发**（委托 :mod:`.material`）：自描述分块下发到
   ``executor/pytest`` 能力候选节点，``pytest.material.missing`` 只补缺块；
   预推的触发者是 UI / api 层（submit 前调 :meth:`push_material`），本插件
   不做自动预推；
2. **inspect 编排**：``pytest.inspect`` 定向单节点（候选同预推、最少在途优先）、
   ``request_id`` 幂等（首个应答胜出）、同 material 并发合并（§3.1）、
   ``inspect_timeout_s`` 超时扫尾（state failed、error="inspect timeout"）、
   迟到应答收下刷新并记 ``late_results_total``；应答 ``pytest.inspect.result``
   经 ``ctx.test_core.put_problems / put_cases`` 落核心；
3. **主端 GC**（§3.2）：``material_retain_days`` 定时清理无引用超期材料
   （MaterialInUse 竞态跳过）。

族内 topic / 消息字段与执行端 ``slave_plugins/executor_pytest/plugin.py``
逐字一致（proto=1，不认识版本拒收，D8）。跨插件交互一律经 cordis：
``ctx.test_core``（材料 / 用例存储门面）、``ctx.comm``（主从通道）、
``ctx.nodes``（可选判空）；不 import 其他插件包，DeliveryPolicy 字面量 dict。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, cast

from cordis_port import Context, Fiber, Service

from common.log_op import log_operation
from common.test_contracts import MaterialInUse, MaterialNotFound

from .api_service import ExecPytestMasterApiService
from .config import ExecPytestMasterConfig
from .material import (
    PROTO,
    TOPIC_INSPECT,
    TOPIC_INSPECT_RESULT,
    TOPIC_MATERIAL_MISSING,
    MaterialPusher,
    NoCandidateNode,
    require_core,
)
from .ui_service import ExecPytestUiService

#: inspect 通道的分析 / 解析模式（与执行端 ``InspectMode`` 同值）
type InspectMode = Literal["analyze", "collect"]

#: at-least-once 投递策略（comm DeliveryPolicy 字面量，不 import comm 包）
_AT_LEAST_ONCE: dict[str, int] = {"min_success": 1}

#: inspect 超时扫尾周期（秒；内存判定，粗粒度足够）
_WATCHDOG_INTERVAL_S: float = 5.0
#: 主端材料 GC 周期（秒，§3.2：每 6 小时）
_GC_INTERVAL_S: float = 6 * 3600.0
#: inspect 历史容量（FIFO 裁剪终态记录，防内存无界）
_INSPECT_HISTORY_LIMIT: int = 200
#: materials_total 仪表的材料清单读取上限（计数近似）
_GAUGE_LIMIT: int = 10000


def _new_id(prefix: str, now: float) -> str:
    """时间有序 id（ULID 风格，同 test_core 先例）。"""
    return f"{prefix}-{int(now * 1000):013d}-{secrets.token_hex(3)}"


def _dict_rows(raw: object) -> list[dict[str, Any]]:
    """应答里的 problems / cases 清单收敛为 dict 列表（非 dict 条目丢弃）。"""
    if not isinstance(raw, list):
        return []
    return [
        dict(cast("Mapping[str, Any]", item))
        for item in cast("list[object]", raw)
        if isinstance(item, Mapping)
    ]


@dataclass
class InspectRecord:
    """inspect 请求记录（在途 + 终态：幂等 / 超时 / 迟到应答查表）。"""

    request_id: str
    material_id: str
    mode: InspectMode
    node_id: str
    started_ts: float
    #: inspecting → done（ok）/ failed（ok=false 或超时）
    state: str = "inspecting"
    ok: bool = False
    problems: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    cases: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    duration_s: float = 0.0
    #: 终结原因（超时 = "inspect timeout"；收到应答后清空）
    error: str = ""
    finished_ts: float = 0.0
    #: 已收过应答（幂等：首个应答胜出）
    result_received: bool = False
    #: 迟到应答（超时终结后才到，§3.1 收下刷新）
    late: bool = False

    def to_dict(self) -> dict[str, Any]:
        """inspect_status 快照（JSON 安全）。"""
        return {
            "request_id": self.request_id,
            "material_id": self.material_id,
            "mode": self.mode,
            "node_id": self.node_id,
            "state": self.state,
            "started_ts": self.started_ts,
            "finished_ts": self.finished_ts,
            "ok": self.ok,
            "problems": [dict(item) for item in self.problems],
            "cases": [dict(item) for item in self.cases],
            "duration_s": self.duration_s,
            "error": self.error,
            "late": self.late,
        }


class ExecPytestMasterPlugin(Service[ExecPytestMasterConfig]):
    """pytest 执行器管理端（``ctx.executor_pytest_master``，DESIGN §2/§5/§6）。

    ``inject`` 注入硬依赖 ``appconfig`` / ``comm``；``ctx.test_core`` 与
    ``ctx.nodes`` 均为**可选依赖**（访问判空）：材料 / 用例存储归核心
    （D10，数据唯一写者），节点目录缺席时无候选节点（预推空跑、inspect 报错）。
    管理 API 拆附属服务 :class:`.api_service.ExecPytestMasterApiService`，
    webapi 缺席不阻塞本插件。
    """

    name: str = "executor-pytest-master"
    provide: str | None = "executor_pytest_master"
    inject: ClassVar[list[str]] = ["appconfig", "comm"]
    Config: Any = ExecPytestMasterConfig

    def __init__(self, ctx: Context, config: ExecPytestMasterConfig | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.executor_pytest_master
        self.ctx: Context = ctx
        self.config: ExecPytestMasterConfig = (
            config if isinstance(config, ExecPytestMasterConfig) else ExecPytestMasterConfig()
        )
        #: 时钟（可注入：测试整体替换本属性）
        self._clock: Callable[[], float] = time.time
        self._pusher: MaterialPusher = MaterialPusher(
            ctx,
            chunk_size=lambda: self.config.push_chunk_bytes,
            concurrency=lambda: self.config.push_concurrency,
            clock=lambda: self._clock(),
        )
        #: request_id → inspect 记录（在途 + 有限历史）
        self._inspects: dict[str, InspectRecord] = {}
        #: material_id → 在途 request_id（§3.1 并发合并）
        self._inspect_by_material: dict[str, str] = {}
        self._tasks: set[asyncio.Task[object]] = set()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._gc_task: asyncio.Task[None] | None = None
        self._api_fiber: Fiber | None = None
        #: 页面服务 fiber：webui 缺席停 PENDING，晚到自动挂载（同 api fiber）
        self._ui_fiber: Fiber | None = None
        #: materials_total 仪表缓存（stats() 是同步接口，周期刷新）
        self._materials_gauge: int = 0
        self._stats: dict[str, int] = {"inspect_failed": 0, "late_results_total": 0}

    # ================================================================ 生命周期
    async def init(self) -> object:
        """注册收件、启动看门狗 / GC 循环、挂管理 API；返回清理函数。"""
        self.ctx.comm.on(TOPIC_MATERIAL_MISSING, self._on_material_missing)
        self.ctx.comm.on(TOPIC_INSPECT_RESULT, self._on_inspect_result)
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        self._watchdog_task.add_done_callback(self._log_task_exit)
        self._gc_task = asyncio.create_task(self._gc_loop())
        self._gc_task.add_done_callback(self._log_task_exit)
        # 附属服务不 await：webapi / webui 缺席时 fiber 停在 PENDING，晚到自动挂载
        self._api_fiber = self.ctx.plugin(ExecPytestMasterApiService)
        self._ui_fiber = self.ctx.plugin(ExecPytestUiService)
        await self._refresh_materials_gauge()
        self.ctx.logger.info(
            "pytest 执行器管理端已启动：分块 %s 字节 / 预推并发 %s / inspect 超时 %s 秒",
            self.config.push_chunk_bytes,
            self.config.push_concurrency,
            self.config.inspect_timeout_s,
        )
        return self._stop

    async def _stop(self) -> None:
        """卸载清理：停看门狗 / GC 循环，取消在途后台任务（gather 吸收自身取消）。"""
        tasks = [task for task in (self._watchdog_task, self._gc_task) if task is not None]
        self._watchdog_task = None
        self._gc_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        spawned = list(self._tasks)
        for task in spawned:
            task.cancel()
        if spawned:
            await asyncio.gather(*spawned, return_exceptions=True)
        self._tasks.clear()

    def _log_task_exit(self, task: asyncio.Task[object]) -> None:
        """后台任务退出的观察哨：意外退出记 error，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("executor-pytest-master 后台任务意外退出：%s", error)

    def _spawn(self, coro: Coroutine[Any, Any, object]) -> None:
        """派生后台任务并登记（卸载统一取消）。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ============================================================ 服务门面（§8）
    async def push_material(
        self,
        material_id: str,
        *,
        selection_ids: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        """预推材料到全部候选节点（UI/api 在 submit 前触发；§5.2 主路径）。"""
        with log_operation(f"push-{material_id}"):
            result = await self._pusher.push_material(material_id, selection_ids=selection_ids)
        await self._refresh_materials_gauge()
        return result

    async def inspect(self, material_id: str, mode: InspectMode) -> str:
        """触发分析 / 解析（定向单节点，返回 request_id）。

        * **并发合并**：同 material 已有 inspect 在途时复用在途 request_id
          （DESIGN §3.1，不并发双跑）；
        * **定向单节点**：候选同预推（``executor/pytest`` 能力），最少在途
          优先（同负载按节点名稳定排序）；无候选记失败并抛
          :class:`~.material.NoCandidateNode`（API 层映射 503）；
        * 应答幂等 / 迟到 / 超时见 ``pytest.inspect.result`` 收件与看门狗。
        """
        if mode not in ("analyze", "collect"):
            raise ValueError(f"mode 只能是 analyze / collect，收到 {mode!r}")
        if not material_id:
            raise ValueError("material_id 必填")
        existing = self._inspect_by_material.get(material_id)
        if existing is not None:
            record = self._inspects.get(existing)
            if record is not None and record.state == "inspecting":
                self.ctx.logger.info(
                    "inspect 合并：%s 复用在途 %s（%s）", material_id, existing, record.mode
                )
                return existing
        inflight: dict[str, int] = {}
        for record in self._inspects.values():
            if record.state == "inspecting":
                inflight[record.node_id] = inflight.get(record.node_id, 0) + 1
        candidates = self._pusher.candidate_nodes()
        if not candidates:
            self._stats["inspect_failed"] += 1
            self.ctx.logger.warning(
                "inspect 无可用候选节点（material=%s mode=%s），记失败", material_id, mode
            )
            raise NoCandidateNode(f"无可用候选执行节点（executor/pytest）：{material_id}")
        candidates.sort(key=lambda node: (inflight.get(node, 0), node))
        node_id = candidates[0]
        now = self._clock()
        request_id = _new_id("insp", now)
        record = InspectRecord(
            request_id=request_id,
            material_id=material_id,
            mode=mode,
            node_id=node_id,
            started_ts=now,
        )
        self._inspects[request_id] = record
        self._inspect_by_material[material_id] = request_id
        self._prune_inspects()
        self.ctx.comm.send_to(
            node_id,
            TOPIC_INSPECT,
            {"proto": PROTO, "request_id": request_id, "material_id": material_id, "mode": mode},
            policy=dict(_AT_LEAST_ONCE),
        )
        self.ctx.logger.info(
            "inspect 已发出：%s → %s（%s %s）", request_id, node_id, material_id, mode
        )
        return request_id

    async def inspect_status(self, request_id: str) -> Mapping[str, Any] | None:
        """inspect 请求状态 / 结果快照；未知 request_id 返回 None。"""
        record = self._inspects.get(request_id)
        return record.to_dict() if record is not None else None

    def stats(self) -> Mapping[str, int]:
        """运行统计（§14；/healthz 健康指标同源）。"""
        return {
            "materials_total": self._materials_gauge,
            "pushing": self._pusher.pushing_count(),
            "push_failed": self._pusher.push_failed,
            "inspecting": sum(
                1 for record in self._inspects.values() if record.state == "inspecting"
            ),
            "inspect_failed": self._stats["inspect_failed"],
            "chunks_resent": self._pusher.chunks_resent,
            "late_results_total": self._stats["late_results_total"],
        }

    # ============================================================ comm 收件（§5/§6）
    def _on_material_missing(self, payload: object, _meta: object) -> None:
        """``pytest.material.missing``：只补缺块（空清单整包补推），后台执行。"""
        self._spawn(self._pusher.resend_missing(payload))

    def _on_inspect_result(self, payload: object, _meta: object) -> None:
        """``pytest.inspect.result``：request_id 幂等首个应答胜出；迟到收下刷新。"""
        if not isinstance(payload, Mapping):
            return
        raw = cast("Mapping[str, Any]", payload)
        proto = raw.get("proto")
        if not isinstance(proto, int) or isinstance(proto, bool) or proto != PROTO:
            self.ctx.logger.warning("pytest.inspect.result proto 不匹配（%s），拒收", proto)
            return
        request_id = str(raw.get("request_id") or "")
        record = self._inspects.get(request_id)
        if record is None:
            self.ctx.logger.warning("pytest.inspect.result 未知 request_id=%s，忽略", request_id)
            return
        if record.result_received:
            return  # 幂等：首个应答胜出（AT_LEAST_ONCE 重复投递丢弃）
        record.result_received = True
        if record.state != "inspecting":
            # 迟到应答（已超时终结，§3.1）：收下刷新并计 warning 口径
            self._stats["late_results_total"] += 1
            record.late = True
            self.ctx.logger.warning(
                "inspect 迟到应答（request_id=%s 已终结为 %s），收下刷新", request_id, record.state
            )
        self._spawn(self._apply_inspect_result(record, raw))

    async def _apply_inspect_result(
        self, record: InspectRecord, raw: Mapping[str, Any]
    ) -> None:
        """应答落核心（put_problems / put_cases）并刷新记录（含迟到刷新）。"""
        ok = bool(raw.get("ok"))
        problems = _dict_rows(raw.get("problems"))
        cases = _dict_rows(raw.get("cases"))
        has_error = any(str(item.get("severity") or "") == "error" for item in problems)
        try:
            core = require_core(self.ctx)
            await core.put_problems(
                record.material_id, problems, state="analyzed" if ok and not has_error else "failed"
            )
            if record.mode == "collect":
                await core.put_cases(record.material_id, cases)
        except MaterialNotFound:
            self.ctx.logger.warning("inspect 应答落库时材料已不存在：%s", record.material_id)
        except Exception as error:  # noqa: BLE001 —— 落库失败不丢应答记录
            self.ctx.logger.error("inspect 应答落库失败：%s", error)
        was_inflight = record.state == "inspecting"
        record.ok = ok
        record.problems = problems
        record.cases = cases
        record.duration_s = float(raw.get("duration_s") or 0.0)
        record.error = ""
        record.state = "done" if ok else "failed"
        record.finished_ts = self._clock()
        self._release_material_slot(record)
        if not ok and was_inflight:
            self._stats["inspect_failed"] += 1
        self.ctx.logger.info(
            "inspect 应答已落库：%s（%s %s → %s，%s 问题）",
            record.request_id,
            record.material_id,
            record.mode,
            record.state,
            len(problems),
        )

    # ============================================================ inspect 看门狗
    def sweep_inspect_timeouts(self) -> int:
        """超时扫尾：state failed、error="inspect timeout"；返回终结数。"""
        now = self._clock()
        finished = 0
        for record in self._inspects.values():
            if record.state != "inspecting":
                continue
            if now - record.started_ts < self.config.inspect_timeout_s:
                continue
            record.state = "failed"
            record.error = "inspect timeout"
            record.finished_ts = now
            self._stats["inspect_failed"] += 1
            self._release_material_slot(record)
            finished += 1
            self.ctx.logger.warning(
                "inspect 超时：%s（material=%s node=%s）",
                record.request_id,
                record.material_id,
                record.node_id,
            )
        return finished

    async def _watchdog_loop(self) -> None:
        """inspect 超时扫尾周期任务。"""
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL_S)
            self.sweep_inspect_timeouts()

    def _release_material_slot(self, record: InspectRecord) -> None:
        """释放 material → request 的在途槽（并发合并表）。"""
        if self._inspect_by_material.get(record.material_id) == record.request_id:
            del self._inspect_by_material[record.material_id]

    def _prune_inspects(self) -> None:
        """inspect 历史 FIFO 裁剪（只裁终态记录，全在途时下一轮再裁）。"""
        while len(self._inspects) > _INSPECT_HISTORY_LIMIT:
            for key, record in list(self._inspects.items()):
                if record.state != "inspecting":
                    del self._inspects[key]
                    break
            else:
                return

    # ============================================================ 主端 GC（§3.2）
    async def _gc_loop(self) -> None:
        """GC 周期任务：启动即跑一轮，之后每 6 小时。"""
        while True:
            try:
                await self._refresh_materials_gauge()
                await self._gc_materials()
            except Exception as error:  # noqa: BLE001 —— 循环体异常不拉垮周期任务
                self.ctx.logger.error("材料 GC 周期失败：%s", error)
            await asyncio.sleep(_GC_INTERVAL_S)

    async def _gc_materials(self) -> list[str]:
        """删无引用超期材料；MaterialInUse / MaterialNotFound 竞态跳过。"""
        core = require_core(self.ctx)
        cutoff = self._clock() - self.config.material_retain_days * 86400.0
        removed: list[str] = []
        for row in await core.list_unreferenced(older_than_ts=cutoff):
            material_id = str(row.get("id") or row.get("material_id") or "")
            if not material_id:
                continue
            try:
                await core.delete_material(material_id)
            except (MaterialInUse, MaterialNotFound):
                continue  # 刚被引用 / 刚被删：本轮跳过
            removed.append(material_id)
        if removed:
            self.ctx.logger.info("材料 GC 清理：%s", ", ".join(removed))
            await self._refresh_materials_gauge()
        return removed

    async def _refresh_materials_gauge(self) -> None:
        """materials_total 仪表刷新（stats() 同步接口的缓存值）。"""
        try:
            rows = await require_core(self.ctx).list_materials(limit=_GAUGE_LIMIT)
        except Exception:  # noqa: BLE001 —— 仪表失败不影响主流程
            return
        self._materials_gauge = len(rows)


__all__: list[str] = ["ExecPytestMasterPlugin", "InspectMode", "InspectRecord"]
