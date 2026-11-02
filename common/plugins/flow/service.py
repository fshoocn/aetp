"""flow 引擎服务（``ctx.flow``，Phase 3 持久化版；§5.2 / §5.3 / §5.9 / §5.10）。

* 一个插件、一个服务：注册表 / 解析 / 预检 / 执行 / 事件 / 并发（§5.1）；
  **不做**业务逻辑、脚本执行、通知渠道、多主一致性；
* 编排只在主节点：从节点调用 :meth:`start` 抛 ``FlowError("本节点不编排")``
  （从节点是 worker：注册步骤、供主节点远程调用——Phase 4）；
* Phase 3 持久化（§5.10）：事件日志为主（``aetp_flow_events``）、runs / steps
  行投影为辅，「先写后做」；重启恢复（幂等步重放、未知步 interrupted 人工
  处置）、幂等键跨重启去重、``retain_days`` / ``retain_max_runs`` 保留清理；
* HTTP API 是独立的 :class:`FlowApiService`（inject flow + webapi，headless
  自然 PENDING——「UI / 页面即服务」约定）。

注册语义（§5.3）：``step()`` / ``define()`` 绑调用方 fiber，卸载自动摘除并派发
``flow/steps-changed``；``start()`` 预检先行（``FlowRejected`` 不产生半个 run），
``idempotency_key`` 去重（内存 + 落库），返回 :class:`RunHandle`。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, cast

from cordis_port import Context, Fiber, Service

from common.capabilities import CAP_EVENT, select_capabilities
from common.cordis_utils import parallel_event
from common.flow_contracts import (
    EVT_RUN_FINISHED,
    EVT_RUN_INTERRUPTED,
    EVT_STEPS_CHANGED,
    STEP_KIND,
    FlowDef,
    FlowError,
    FlowRejected,
    InsertSpec,
    JsonValue,
    NodeState,
    Overrides,
    Problem,
    ProblemCode,
    ProblemLevel,
    RemoteResult,
    RunHandle,
    RunState,
    StepContext,
    StepHandler,
    StepRegistration,
)

from .config import FlowConfig
from .executor import FlowExecutor, Run, RunHandleImpl
from .preflight import (
    apply_overrides,
    flow_def_to_json,
    parse_flow_def,
    preflight_flow,
    validate_flow_def,
)
from .registry import StepRecord, StepRegistry
from .remote import RemoteHub, WorkerRuntime
from .store import FlowStore, nodes_from_json


def _fiber_name(fiber: Fiber | None) -> str:
    """fiber 名（诊断 / provider 归属用）；无 fiber 回退 ``"root"``。"""
    return getattr(fiber, "name", None) or "root"


#: 用户流程导出 / 导入信封（§5.14.4；附录 D）
USER_FLOW_SCHEMA = "aetp.flow/1"


def problem_to_dict(problem: Problem) -> dict[str, str]:
    return {
        "level": problem.level.value,
        "node": problem.node,
        "code": problem.code.value,
        "message": problem.message,
    }


def _merge_overrides(
    base: Overrides | None, extra: Overrides | None
) -> Overrides | None:
    """单次运行覆盖叠加在持久化覆盖之上（§5.14.1 同构语义）。

    ``disable`` 取并集；``insert`` 按插入节点 id 后者覆盖；``params`` / ``pin``
    / ``labels`` 按节点逐键深合并（后写胜）。
    """
    if not base:
        return extra
    if not extra:
        return base
    merged: dict[str, Any] = {
        "disable": list(
            dict.fromkeys([*base.get("disable", []), *extra.get("disable", [])])
        )
    }
    inserts: dict[str, InsertSpec] = {}
    for spec in [*base.get("insert", []), *extra.get("insert", [])]:
        node = dict(spec.get("node") or {})
        inserts[str(node.get("id") or "")] = dict(spec)  # type: ignore[assignment]
    merged["insert"] = list(inserts.values())
    for key in ("params", "pin", "labels"):
        layer: dict[str, Any] = {
            node_id: dict(value) if isinstance(value, dict) else value
            for node_id, value in base.get(key, {}).items()
        }
        for node_id, value in extra.get(key, {}).items():
            if isinstance(value, dict) and isinstance(layer.get(node_id), dict):
                layer[node_id] = {**layer[node_id], **value}
            else:
                layer[node_id] = value
        merged[key] = layer
    return cast(Overrides, merged)


def _insert_present(overrides: Overrides | None, node_id: str) -> bool:
    for spec in (overrides or {}).get("insert", []):
        node = dict(spec.get("node") or {})
        if str(node.get("id") or "") == node_id:
            return True
    return False


class FlowService(Service[FlowConfig]):
    """流程引擎（``ctx.flow``）：注册 / 预检 / 执行 / 事件 / 并发。"""

    #: 服务名（即 ``ctx.flow``）
    provide: str | None = "flow"
    #: 插件名
    name: str = "flow"
    #: db 是持久化地基（§5.10 事件日志住进 ``aetp.db``）；comm / nodes 是远程
    #: 执行（§5.11）的**可选**依赖（从节点没有 nodes、无头没有 comm）——
    #: 访问判空、缺了只禁远程；webapi 不在此列（API 是独立的 FlowApiService）
    inject: ClassVar[dict[str, list[str]]] = {
        "required": ["appconfig", "node_kind", "node_name", "db"],
        "optional": ["comm", "nodes"],
    }
    Config: type[FlowConfig] = FlowConfig

    def __init__(self, ctx: Context, config: FlowConfig | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.flow
        self.ctx: Context = ctx
        self._provided_ctx: Context = ctx
        self.config: FlowConfig = (
            config if isinstance(config, FlowConfig) else FlowConfig()
        )
        self._registry = StepRegistry(
            cancel_grace=self.config.cancel_grace, on_change=self._on_steps_changed
        )
        self._stats: dict[str, int] = {
            "runs_started": 0,
            "runs_succeeded": 0,
            "runs_failed": 0,
            "runs_cancelled": 0,
            "runs_interrupted": 0,
            "preflight_rejects": 0,
            "resolve_misses": 0,
            "drain_timeouts": 0,
            "remote_jobs": 0,
            "remote_dispatched": 0,
            "remote_retried": 0,
            "remote_lost": 0,
            "remote_unknown": 0,
        }
        self._store: FlowStore | None = None  # init() 接 db 后就位（_make_executor 读取）
        self._executor = self._make_executor()
        self._runs: dict[str, Run] = {}
        self._idempotency: dict[str, str] = {}
        #: 用户流程缓存（§5.14.4：单主节点；写路径（保存 / 删除 / 重校验）即时刷新）
        self._user_flows: dict[str, dict[str, Any]] = {}
        #: 附加项注册表（§5.14.3：插件声明、默认关闭、只进编辑器建议）
        self._attachments: dict[str, dict[str, Any]] = {}
        self._slots = asyncio.Semaphore(self.config.max_concurrent_runs)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._api_fiber: Fiber | None = None
        self._ui_fiber: Fiber | None = None
        self._worker: WorkerRuntime | None = None
        self._hub: RemoteHub | None = None
        self._cap_dispose: Callable[[], None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None

    def _make_executor(self) -> FlowExecutor:
        return FlowExecutor(
            self._registry.records,
            self.config,
            dispatch=self._dispatch,
            node_name=str(getattr(self._provided_ctx, "node_name", "unknown")),
            recorder=self._store,
            stats=self._stats,
        )

    async def init(self) -> Callable[[], Awaitable[None]]:
        """持久化 + 远程接线：建表 → 恢复 → 保留清理 → 远程 / 能力发布 → API。

        无常驻后台任务；卸载清理 = 取消在途运行（§5.12「引擎自身卸载」）。
        """
        if getattr(self._provided_ctx, "db", None) is not None:
            self._store = FlowStore(self._provided_ctx.db)
            await self._store.sync()
            self._executor = self._make_executor()  # 带 recorder 重建
            # 恢复走后台延迟任务（resume_delay，§5.10 空窗容忍）：不阻塞启动
            self._recovery_task = asyncio.create_task(self._recover_later())
            self._recovery_task.add_done_callback(self._log_task_exit)
            await self._retain_once()
            await self._load_user_flows()  # 用户流程缓存 + 模板重校验（§5.14.1）
        comm = self._provided_ctx.get("comm")
        if comm is not None:
            # 远程执行（§5.11）：worker 侧收发 + 主侧协调器；无 comm 只禁远程
            node_name = str(getattr(self._provided_ctx, "node_name", "unknown"))
            self._worker = WorkerRuntime(
                comm=comm, records=self._registry.records, config=self.config,
                node_name=node_name, logger=self._provided_ctx.logger,
            )
            self._hub = RemoteHub(
                comm=comm,
                nodes_getter=lambda: self._provided_ctx.get("nodes"),
                config=self.config, node_name=node_name,
                logger=self._provided_ctx.logger,
                on_unknown=self._interrupt_for_remote, stats=self._stats,
            )
        # 能力发布（§5.11.2）：本机步骤 → flow.step 信封（CAP_EVENT 收集桶）
        self._cap_dispose = self._provided_ctx.on(
            CAP_EVENT, self._contribute_capabilities
        )
        # API 子服务（inject flow + webapi）：无 webapi 自然 PENDING，到位自动加载
        from .api import FlowApiService

        self._api_fiber = self.ctx.plugin(FlowApiService)
        # 页面子服务（inject flow + webui）：「流程」编辑器页面（§5.14.8）
        from .ui_service import FlowUiService

        self._ui_fiber = self.ctx.plugin(FlowUiService)

        async def cleanup() -> None:
            if self._recovery_task is not None:
                self._recovery_task.cancel()
                self._recovery_task = None
            if self._worker is not None:
                self._worker.close()
                self._worker = None
            if self._hub is not None:
                self._hub.close()
                self._hub = None
            if self._cap_dispose is not None:
                self._cap_dispose()
                self._cap_dispose = None
            for run in self._runs.values():
                if not run.finished.is_set():
                    run.cancelled.set()
                    for sc in list(run.inflight.values()):
                        sc.cancelled.set()
            tasks = list(self._tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._api_fiber is not None:
                self._api_fiber.dispose()
                self._api_fiber = None
            if self._ui_fiber is not None:
                self._ui_fiber.dispose()
                self._ui_fiber = None

        return cleanup

    # ---------------------------------------------------------- 贡献者 API
    def step(
        self,
        stage: str,
        step_id: str,
        handler: StepHandler,
        *,
        labels: Mapping[str, str] | None = None,
        priority: int = 0,
        version: str = "",
        summary: str = "",
        params_schema: Mapping[str, JsonValue] | None = None,
        max_concurrency: int = 0,
        idempotent: bool = False,
    ) -> StepRegistration:
        """注册步骤（§5.3）：绑调用方 fiber，卸载即摘；返回注册句柄。"""
        caller = self._caller_fiber()
        provider = _fiber_name(caller).split(":")[0]  # 具名守护取冒号前（§3.5.2）
        handle = self._registry.register_step(
            stage=stage,
            step_id=step_id,
            handler=handler,
            labels=dict(labels or {}),
            priority=priority,
            version=version,
            summary=summary,
            params_schema=params_schema,
            max_concurrency=max_concurrency,
            idempotent=idempotent,
            provider=provider,
            fiber=caller,
        )
        caller.effect(lambda: lambda: handle.close(), f"ctx.flow.step({stage}/{step_id})")
        return handle

    def define(self, flow: FlowDef) -> Callable[[], None]:
        """注册流程模板（绑 fiber）；同 id 冲突抛 ``ValueError``（§5.3）。"""
        validate_flow_def(flow)
        caller = self._caller_fiber()
        dispose = self._registry.register_flow(flow, caller)
        caller.effect(lambda: dispose, f"ctx.flow.define({flow.id!r})")
        # 模板升级（§5.14.1）：引用它的用户流程覆盖自动重新校验（needs_attention）
        task = asyncio.create_task(self._revalidate_user_flows(flow.id))
        task.add_done_callback(self._log_task_exit)
        return dispose

    def catalog(
        self,
        *,
        stage: str | None = None,
        labels: Mapping[str, str] | None = None,
        q: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, object]]:
        """步骤目录 = 能力信封列表（``kind = "flow.step"``）。

        筛选：``stage`` / ``labels``（子集匹配）/ ``q``（id / summary 模糊）；
        ``scope="cluster"`` 合并从节点能力（``nodes`` 服务，§5.11.2）。
        """
        wanted: dict[str, str] = {}
        if stage is not None:
            wanted["stage"] = stage
        if labels:
            wanted.update(dict(labels))
        caps = [record.envelope() for record in self._registry.alive_records()]
        picked = select_capabilities(caps, kinds=[STEP_KIND], labels=wanted or None)
        local_node = str(getattr(self._provided_ctx, "node_name", "") or "")
        items: list[dict[str, object]] = []
        for cap in picked:
            entry = cap.to_dict()
            entry["node_id"] = local_node
            items.append(entry)
        if scope == "cluster":
            nodes = self._provided_ctx.get("nodes")
            if nodes is not None:
                try:
                    remote = nodes.select(
                        kinds=[STEP_KIND], labels=wanted or None, online_only=False
                    )
                except Exception as exc:  # noqa: BLE001 —— nodes 故障不拖垮目录
                    self._provided_ctx.logger.error("flow 集群目录合并失败：%s", exc)
                    remote = []
                for cap in remote:
                    entry = {
                        key: cap.get(key)
                        for key in ("kind", "id", "provider", "version", "labels", "summary", "detail")
                    }
                    entry["node_id"] = cap.get("node_id") or ""
                    items.append(entry)
        if q:
            needle = q.lower()
            items = [
                item
                for item in items
                if needle in str(item.get("id") or "").lower()
                or needle in str(item.get("summary") or "").lower()
            ]
        return items

    def preflight(
        self, flow_id: str, overrides: Overrides | None = None
    ) -> list[Problem]:
        """预检（§5.7）：结构 + 覆盖 + 当前能力；只给问题清单，不产生 run。"""
        flow, persisted, _version = self._resolve_flow(flow_id)
        return preflight_flow(
            flow, _merge_overrides(persisted, overrides), self._registry.records()
        )

    # ------------------------------------------- 用户流程（Phase 5 / §5.14）
    async def save_flow(
        self, flow_id: str, body: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        """保存用户流程（§5.14.4）：每次生成不可变新版本。

        形态二选一（``$schema: aetp.flow/1``）：``extends + overrides``
        （模板 + 持久化覆盖）或 ``def``（完整定义，无 locked 语义）。结构 /
        语义 / 参数错抛 ``FlowRejected``（422）或 ``FlowError``（400）；
        **当前无提供者只给 warn**（§5.14.5，启动时再硬校验）。
        """
        if self._store is None:
            raise FlowError("未启用持久化，无法保存用户流程")
        if not isinstance(body, Mapping):
            raise FlowError("请求体不是流程定义")
        if self._registry.flow(flow_id) is not None:
            raise FlowError(f"与模板同名，不可保存为用户流程：{flow_id!r}")
        schema = body.get("$schema")
        if schema is not None and str(schema) != USER_FLOW_SCHEMA:
            raise FlowError(f"不支持的 $schema：{schema!r}")
        extends = str(body.get("extends") or "")
        if extends:
            template = self._registry.flow(extends)
            if template is None:
                raise FlowError(f"模板不存在：{extends!r}")
            raw_overrides = body.get("overrides") or {}
            if not isinstance(raw_overrides, Mapping):
                raise FlowError("overrides 必须是对象")
            overrides: Overrides = dict(raw_overrides)  # type: ignore[assignment]
            _nodes, _disabled, problems = apply_overrides(template, overrides)
            # 参数须符合当前提供者 schema（§5.14.2）；缺提供者仅 warn 不拦保存
            problems += [
                p
                for p in preflight_flow(template, overrides, self._registry.records())
                if p.code is ProblemCode.BAD_PARAMS and p not in problems
            ]
            errors = [p for p in problems if p.level is ProblemLevel.ERROR]
            if errors:
                raise FlowRejected(errors)
            stored: dict[str, Any] = {
                "title": str(body.get("title") or template.title),
                "overrides": dict(overrides),
            }
        else:
            def_raw = body.get("def")
            if not isinstance(def_raw, Mapping):
                raise FlowError("用户流程须提供 extends+overrides 或 def")
            try:
                flow_def = parse_flow_def(dict(def_raw))
            except (TypeError, ValueError) as exc:
                raise FlowError(f"流程定义不合法：{exc}") from exc
            if flow_def.id != flow_id:
                raise FlowError(f"def.id 与路径不一致：{flow_def.id!r} != {flow_id!r}")
            stored = {
                "title": flow_def.title or str(body.get("title") or ""),
                "def": flow_def_to_json(flow_def),
            }
        row = await self._store.save_def(
            flow_id, stored, extends=extends, actor=actor,
            enabled=bool(body.get("enabled", True)),
        )
        await self._revalidate_user_flows()  # 刷新缓存（新版本即最新）
        detail = await self.flow_detail(flow_id)
        self._audit("flow.save", flow_id, actor, f"version={row['version']}")
        return detail

    async def delete_flow(self, flow_id: str, *, actor: str = "") -> None:
        """删除用户流程（全部版本；历史 run 不受影响）。模板返回 409 语义。"""
        if self._store is None:
            raise FlowError("未启用持久化")
        if self._registry.flow(flow_id) is not None:
            raise FlowError(f"模板不可删除：{flow_id!r}")
        if flow_id not in self._user_flows:
            raise FlowError(f"流程不存在：{flow_id!r}")
        await self._store.delete_def(flow_id)
        self._user_flows.pop(flow_id, None)
        self._audit("flow.delete", flow_id, actor)

    def list_flows(self) -> list[dict[str, Any]]:
        """流程列表（模板 + 用户流程最新版本，附录 D）。"""
        items: list[dict[str, Any]] = [
            {
                "id": flow.id, "kind": "builtin", "title": flow.title,
                "extends": "", "version": flow.version,
                "enabled": True, "needs_attention": False,
                "created_by": "", "created_at": 0.0,
            }
            for flow in self._registry.flows()
        ]
        for entry in self._user_flows.values():
            row = entry["row"]
            items.append(
                {
                    "id": row["id"], "kind": "user", "title": row["title"],
                    "extends": entry["extends"], "version": row["version"],
                    "enabled": row["enabled"], "needs_attention": row["needs_attention"],
                    "created_by": row["created_by"], "created_at": row["created_at"],
                }
            )
        return sorted(items, key=lambda item: str(item["id"]))

    async def flow_detail(self, flow_id: str) -> dict[str, Any]:
        """流程详情（附录 D）：模板或用户流程（含全部版本 + 生效节点 + 导出体）。"""
        entry = self._user_flows.get(flow_id)
        if entry is not None:
            assert self._store is not None
            rows = [r for r in await self._store.defs() if r["id"] == flow_id]
            row = entry["row"]
            base_flow = entry["flow"]
            return {
                "id": flow_id, "kind": "user", "title": row["title"],
                "extends": entry["extends"], "version": row["version"],
                "enabled": row["enabled"], "needs_attention": row["needs_attention"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "overrides": dict(entry["overrides"] or {}),
                "flow": flow_def_to_json(base_flow),
                "effective_nodes": self._effective_nodes(base_flow, entry["overrides"]),
                "problems": [problem_to_dict(p) for p in entry["problems"]],
                "versions": [
                    {
                        "version": r["version"], "created_by": r["created_by"],
                        "created_at": r["created_at"], "enabled": r["enabled"],
                        "needs_attention": r["needs_attention"],
                    }
                    for r in sorted(rows, key=lambda r: int(r["version"]))
                ],
                "export": {
                    "$schema": USER_FLOW_SCHEMA, "id": flow_id, **dict(row["body"]),
                },
            }
        flow = self._registry.flow(flow_id)
        if flow is None:
            raise FlowError(f"流程未注册：{flow_id!r}")
        return {
            "id": flow_id, "kind": "builtin", "title": flow.title,
            "extends": "", "version": flow.version,
            "enabled": True, "needs_attention": False,
            "created_by": "", "created_at": 0.0,
            "overrides": {}, "flow": flow_def_to_json(flow),
            "effective_nodes": self._effective_nodes(flow, None),
            "problems": [], "versions": [],
            "export": {"$schema": USER_FLOW_SCHEMA, "id": flow_id, "def": flow_def_to_json(flow)},
        }

    # ------------------------------------------------------ 附加项（§5.14.3）
    def attachment(
        self,
        flow_id: str,
        slot: str,
        node_id: str,
        stage: str,
        *,
        labels: Mapping[str, str] | None = None,
        params: Mapping[str, JsonValue] | None = None,
        summary: str = "",
    ) -> Callable[[], None]:
        """附加项声明：「我适合插在某流程的某插槽」——绑 fiber、**默认关闭**。

        只进编辑器建议列表；管理员显式启用后才写入用户流程（新版本），
        安装插件不悄悄改变既有流程行为（§5.14.3）。
        """
        attachment_id = f"{flow_id}:{slot}:{node_id}"
        if attachment_id in self._attachments:
            raise ValueError(f"附加项已注册：{attachment_id!r}")
        caller = self._caller_fiber()
        self._attachments[attachment_id] = {
            "id": attachment_id, "flow_id": flow_id, "slot": slot,
            "node_id": node_id, "stage": stage,
            "labels": dict(labels or {}), "params": dict(params or {}),
            "summary": summary, "fiber": caller,
        }

        def dispose() -> None:
            self._attachments.pop(attachment_id, None)

        caller.effect(lambda: dispose, f"ctx.flow.attachment({attachment_id!r})")
        return dispose

    def attachments(self) -> list[dict[str, Any]]:
        """附加项建议列表（存活注册；``enabled_in`` = 已写入的用户流程）。"""
        items: list[dict[str, Any]] = []
        for record in self._attachments.values():
            if record["fiber"].uid is None:
                continue
            items.append(
                {
                    "id": record["id"], "flow_id": record["flow_id"],
                    "slot": record["slot"], "node_id": record["node_id"],
                    "stage": record["stage"], "labels": dict(record["labels"]),
                    "params": dict(record["params"]), "summary": record["summary"],
                    "enabled_in": [
                        flow_id
                        for flow_id, entry in self._user_flows.items()
                        if entry["extends"] == record["flow_id"]
                        and _insert_present(entry["overrides"], record["node_id"])
                    ],
                }
            )
        return sorted(items, key=lambda item: str(item["id"]))

    async def set_attachment_enabled(
        self, attachment_id: str, flow_id: str, enabled: bool, *, actor: str = ""
    ) -> dict[str, Any]:
        """启用 / 停用附加项：写入 / 移除用户流程的插入覆盖（生成新版本）。"""
        record = self._attachments.get(attachment_id)
        if record is None:
            raise FlowError(f"附加项不存在：{attachment_id!r}")
        entry = self._user_flows.get(flow_id)
        if entry is None or not entry["extends"]:
            raise FlowError(f"仅「模板 + 覆盖」型用户流程可挂附加项：{flow_id!r}")
        if entry["extends"] != record["flow_id"]:
            raise FlowError(
                f"附加项属于 {record['flow_id']!r}，与用户流程模板 {entry['extends']!r} 不符"
            )
        template = self._registry.flow(record["flow_id"])
        slot = next(
            (s for s in (template.slots if template else ()) if s.id == record["slot"]), None
        )
        if slot is None:
            raise FlowError(f"插槽不存在：{record['slot']!r}")
        body = dict(entry["row"]["body"])
        body["extends"] = entry["extends"]  # extends 住独立列，回存时补回 body
        overrides = dict(body.get("overrides") or {})
        inserts = [
            dict(spec)
            for spec in overrides.get("insert", [])
            if str((dict(spec).get("node") or {}).get("id")) != record["node_id"]
        ]
        if enabled:
            inserts.append(
                {
                    "after": slot.after,
                    "node": {
                        "id": record["node_id"], "stage": record["stage"],
                        "labels": dict(record["labels"]), "params": dict(record["params"]),
                    },
                }
            )
        overrides["insert"] = inserts
        body["overrides"] = overrides
        detail = await self.save_flow(flow_id, body, actor=actor)  # 不可变新版本
        self._audit(
            "flow.attachment", f"{attachment_id}@{flow_id}", actor,
            "enabled" if enabled else "disabled",
        )
        return detail

    # ------------------------------------------------------------- 发起方 API
    async def start(
        self,
        flow_id: str,
        input: Mapping[str, JsonValue] | None = None,
        *,
        overrides: Overrides | None = None,
        idempotency_key: str | None = None,
        actor: str | None = None,
    ) -> RunHandle:
        """启动运行（§5.9）：预检先行；``FlowRejected`` 不产生半个 run。"""
        if str(getattr(self._provided_ctx, "node_kind", "")) != "master":
            raise FlowError("本节点不编排")
        if idempotency_key is not None:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None and existing in self._runs:
                return RunHandleImpl(self._runs[existing])
            if self._store is not None:  # 跨重启幂等（§5.10：idempotency_key 唯一）
                row = await self._store.get_run_by_key(idempotency_key)
                if row is not None:
                    shell = self._shell_run(row)
                    if shell is None:
                        raise FlowError(f"持久化 run 状态不可读：{row.get('state')!r}")
                    return RunHandleImpl(shell)
        flow, persisted, flow_version = self._resolve_flow(flow_id)
        overrides = _merge_overrides(persisted, overrides)
        problems = preflight_flow(flow, overrides, self._registry.records())
        if any(problem.level is ProblemLevel.ERROR for problem in problems):
            self._stats["preflight_rejects"] += 1
            raise FlowRejected(problems)
        payload = dict(input or {})
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self.config.max_input_bytes:
            raise FlowError(f"运行输入超限（{len(encoded)} > {self.config.max_input_bytes}）")
        nodes, disabled, _extra = apply_overrides(flow, overrides)
        run_id = f"run-{secrets.token_hex(3)}"
        run = Run(
            id=run_id,
            flow_id=flow_id,
            flow_version=flow_version,
            input=payload,
            overrides=overrides,
            nodes=tuple(nodes),
            disabled=frozenset(disabled),
            initiator=actor or _fiber_name(self._caller_fiber()),
            idempotency_key=idempotency_key or "",
        )
        if self._store is not None:  # 先写后做（§5.10）：run-created 落库后才起跑
            await self._store.record_run_created(run, actor=actor or "")
        self._runs[run_id] = run
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = run_id
        self._stats["runs_started"] += 1
        self._tasks[run_id] = asyncio.create_task(self._run(run))
        return RunHandleImpl(run)

    async def cancel(
        self, run_id: str, reason: str = "", *, actor: str | None = None
    ) -> bool:
        """取消运行（§5.8.4）：在途尝试收到信号；终态 run 返回 ``False``。"""
        run = self._runs.get(run_id)
        if run is None or run.finished.is_set():
            return False
        if run.state not in (RunState.PENDING, RunState.RUNNING):
            return False  # 状态已终态（finished.set 稍后）：同样拒绝，409 语义稳定
        if self._store is not None:
            await self._store.record_cancel_requested(
                run_id, reason=reason, actor=actor or ""
            )
        run.cancelled.set()
        for sc in list(run.inflight.values()):
            sc.cancelled.set()
        return True

    async def wait_run(self, run_id: str, timeout: float | None = None) -> RunState | None:
        """等待运行到终态（内存 run 即等；仅落库 run 返回 None，用 :meth:`status`）。"""
        run = self._runs.get(run_id)
        if run is None:
            return None
        handle = RunHandleImpl(run)
        return await handle.wait(timeout)

    # ------------------------------------------------------- 查询 / 处置 API
    async def status(self, run_id: str) -> dict[str, Any] | None:
        """run + 节点时间线（内存优先，落库兜底；§5.9 的 ``run.status()``）。"""
        run = self._runs.get(run_id)
        if run is not None:
            return self._run_status(run)
        if self._store is None:
            return None
        row = await self._store.get_run(run_id)
        if row is None:
            return None
        per_node: dict[str, dict[str, Any]] = {}
        for step in await self._store.steps(run_id):
            node_id = str(step["node_id"])
            entry = per_node.setdefault(
                node_id,
                {
                    "node_id": node_id, "state": "pending", "provider": "",
                    "step": "", "attempts": 0, "error": "", "reason": "",
                    "exec_node": "", "duration_ms": 0.0,
                },
            )
            entry["state"] = step["state"]
            entry["provider"] = step["provider"]
            entry["exec_node"] = step["exec_node"]
            entry["attempts"] = max(int(entry["attempts"]), int(step["attempt"]))
            if step["error"]:
                entry["error"] = step["error"]
            if step["started_at"] and step["finished_at"]:
                entry["duration_ms"] += (step["finished_at"] - step["started_at"]) * 1000.0
        return {
            "run_id": row["id"], "flow_id": row["flow_id"],
            "flow_version": row["flow_version"], "state": row["state"],
            "initiator": row["initiator"], "idempotency_key": row["idempotency_key"],
            "error": row["error"], "warnings": [],
            "input": row["input"], "overrides": row["overrides"],
            "bindings": row["bindings"], "finished": row["state"] not in ("pending", "running"),
            "duration_ms": (row["finished_at"] - row["started_at"]) * 1000.0,
            "nodes": list(per_node.values()),
        }

    async def list_runs(
        self, *, flow_id: str | None = None, state: str | None = None,
        from_ts: float | None = None, to_ts: float | None = None,
        limit: int = 20, offset: int = 0,
    ) -> dict[str, Any]:
        """run 列表（新 → 旧，分页 + 时间窗）；数据源是 runs 行投影（§5.10）。"""
        if self._store is None:
            items = [self._run_status(run) for run in reversed(self._runs.values())]
            return {"runs": items[offset : offset + limit], "total": len(items)}
        rows, total = await self._store.list_runs(
            flow_id=flow_id, state=state, from_ts=from_ts, to_ts=to_ts,
            limit=limit, offset=offset,
        )
        return {"runs": rows, "total": total}

    async def resume(
        self, run_id: str, mode: str = "retry", *, actor: str | None = None
    ) -> bool:
        """人工处置 interrupted run（§5.10 / Q11）：retry 重放 / skip 跳过 / abort 终止。

        ``retry``：unknown 节点重置为 pending 重放（attempt 序号接续）；
        ``skip``：unknown 节点记 skipped 继续后续；``abort``：run 落 failed 终态。
        """
        if mode not in ("retry", "skip", "abort"):
            raise FlowError(f"未知处置模式：{mode!r}")
        if self._store is None:
            raise FlowError("未启用持久化，无法恢复")
        row = await self._store.get_run(run_id)
        if row is None:
            raise FlowError(f"运行不存在：{run_id!r}")
        if row["state"] != RunState.INTERRUPTED.value:
            raise FlowError(f"运行状态不可处置：{row['state']}")
        await self._store.record_resume_requested(run_id, mode=mode, actor=actor or "")
        rebuilt = await self._rebuild_run(run_id)
        if rebuilt is None:
            raise FlowError(f"运行不可重建：{run_id!r}")
        run, _crashed = rebuilt
        if mode == "abort":
            run.state = RunState.FAILED
            run.error = "resume_abort"
            run.finished_at = time.monotonic()
            await self._store.record_run_finished(run)
            await self._dispatch(
                EVT_RUN_FINISHED,
                {
                    "run_id": run.id, "flow_id": run.flow_id,
                    "flow_version": run.flow_version,
                    "state": run.state.value, "duration_ms": run.duration_ms(),
                    "warnings": [], "initiator": run.initiator,
                },
            )
            self._stats["runs_failed"] += 1
            return True
        for node_id, nr in run.node_runs.items():
            if nr.state is NodeState.UNKNOWN:  # 崩溃在途集（重建时已标）
                if mode == "skip":
                    nr.state = NodeState.SKIPPED
                    nr.reason = "resume_skip"
                    await self._store.record_node_finished(run, node_id, nr)
                else:
                    nr.state = NodeState.PENDING  # retry：attempt 接续重放
        self._runs[run_id] = run
        self._stats["runs_started"] += 1
        self._tasks[run_id] = asyncio.create_task(self._run(run))
        return True

    def metrics(self) -> dict[str, float]:
        """指标快照（健康探针 flow 项；§5.13 的计数口径）。"""
        active = sum(1 for run in self._runs.values() if not run.finished.is_set())
        inflight = sum(len(run.inflight) for run in self._runs.values())
        providers = len({record.provider for record in self._registry.alive_records()})
        snapshot = {key: float(value) for key, value in self._stats.items()}
        snapshot["runs_active"] = float(active)
        snapshot["steps_inflight"] = float(inflight)
        snapshot["providers"] = float(providers)
        return snapshot

    # ------------------------------------------------------ 远程作业（Phase 4）
    async def run_remote(
        self,
        sc: StepContext,
        *,
        stage: str,
        where: Mapping[str, str] | None = None,
        jobs: Sequence[Mapping[str, JsonValue]],
        max_parallel: int = 8,
        retries: int = 0,
        timeout: float = 1800.0,
    ) -> list[RemoteResult]:
        """远程作业原语（§5.11.3）：引擎管机制，调用方（提供者）管策略。

        缺 comm（无头）时抛 ``FlowError``；缺 nodes 时作业按 ``place_wait``
        等待后 ``failed(no_capable_node)``。
        """
        if self._hub is None:
            raise FlowError("远程执行需要 comm 服务（本节点未启用）")
        return await self._hub.run(
            sc, stage=stage, where=where, jobs=jobs,
            max_parallel=max_parallel, retries=retries, timeout=timeout,
        )

    async def _interrupt_for_remote(
        self, run_id: str, node_id: str, job_id: str, reason: str
    ) -> None:
        """远程未知失联 → 该节点记 unknown、run 打断（§5.11.5，同 §5.10 语义）。"""
        run = self._runs.get(run_id)
        if run is None or run.finished.is_set():
            return
        run.interrupted_reason = run.interrupted_reason or reason
        run.unknown_node_ids.add(node_id)
        self._stats["runs_interrupted"] = self._stats.get("runs_interrupted", 0) + 1
        if self._store is not None:
            await self._store.record_run_interrupted(
                run_id, node_id=node_id, reason=reason
            )
        await self._dispatch(
            EVT_RUN_INTERRUPTED,
            {"run_id": run_id, "node_id": node_id, "reason": reason},
        )

    def _contribute_capabilities(self, items: list[dict[str, object]]) -> None:
        """CAP_EVENT 发布（§5.11.2）：本机已注册步骤 → ``flow.step`` 信封。"""
        for record in self._registry.alive_records():
            items.append(record.envelope().to_dict())

    # ------------------------------------------------------------- 内部
    async def _run(self, run: Run) -> None:
        try:
            async with self._slots:  # max_concurrent_runs：超出的保持 pending（FIFO）
                await self._executor.execute(run)
            if run.state is RunState.SUCCEEDED:
                self._stats["runs_succeeded"] += 1
            elif run.state is RunState.CANCELLED:
                self._stats["runs_cancelled"] += 1
            elif run.state in (RunState.FAILED, RunState.INTERRUPTED):
                self._stats["runs_failed"] += 1
            await self._retain_once()  # 顺带双限清理（§5.10；仍在任务账内可被取消）
        finally:
            self._tasks.pop(run.id, None)  # 最后销账：卸载清理不漏掉在途尾声

    @staticmethod
    def _run_status(run: Run) -> dict[str, Any]:
        nodes = [
            {
                "node_id": nr.node.id, "state": nr.state.value,
                "provider": nr.provider, "step": nr.step,
                "attempts": nr.attempts, "error": nr.error, "reason": nr.reason,
                "exec_node": nr.exec_node,
                "duration_ms": (
                    (nr.finished_at - nr.started_at) * 1000.0
                    if nr.started_at and nr.finished_at else 0.0
                ),
            }
            for nr in run.node_runs.values()
        ]
        return {
            "run_id": run.id, "flow_id": run.flow_id,
            "flow_version": run.flow_version, "state": run.state.value,
            "initiator": run.initiator, "idempotency_key": run.idempotency_key,
            "error": run.error, "warnings": list(run.warnings),
            "input": dict(run.input), "overrides": dict(run.overrides or {}),
            "bindings": dict(run.bindings), "finished": run.finished.is_set(),
            "duration_ms": float(run.duration_ms()), "nodes": nodes,
        }

    @staticmethod
    def _shell_run(row: Mapping[str, Any]) -> Run | None:
        """已落库 run 的内存壳（幂等去重的返回句柄；终态即等即回）。"""
        try:
            state = RunState(str(row["state"]))
        except ValueError:
            return None
        run = Run(
            id=str(row["id"]), flow_id=str(row["flow_id"]),
            flow_version=int(row["flow_version"] or 0),
            input=dict(row.get("input") or {}), overrides=None, nodes=(),
            initiator=str(row.get("initiator") or ""),
            idempotency_key=str(row.get("idempotency_key") or ""),
        )
        run.state = state
        run.error = str(row.get("error") or "")
        if state is not RunState.PENDING and state is not RunState.RUNNING:
            run.finished.set()
        return run

    def _find_record(self, provider: str, step_id: str) -> StepRecord | None:
        for record in self._registry.records():
            if record.provider == provider and record.step_id == step_id:
                return record
        return None

    async def _rebuild_run(self, run_id: str) -> tuple[Run, list[str]] | None:
        """从事件 + 投影重建 run（§5.10 恢复）：黑板 / attempt 接续 / 在途节点集。"""
        assert self._store is not None
        row = await self._store.get_run(run_id)
        if row is None:
            return None
        events = await self._store.events(run_id)
        created = next((e for e in events if e["type"] == "run-created"), None)
        if created is None:
            return None
        payload = created["payload"]
        run = Run(
            id=run_id,
            flow_id=str(payload.get("flow_id") or row["flow_id"]),
            flow_version=int(payload.get("flow_version") or 0),
            input=dict(payload.get("input") or {}),
            overrides=None,
            nodes=nodes_from_json(str(payload.get("nodes") or "[]")),
            disabled=frozenset(payload.get("disabled") or ()),
            initiator=str(payload.get("initiator") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )
        for step in await self._store.steps(run_id):
            node_id = str(step["node_id"])
            if node_id not in run.node_runs:
                continue
            attempt = int(step["attempt"])
            run.attempt_offsets[node_id] = max(
                run.attempt_offsets.get(node_id, 0), attempt
            )
            run.node_runs[node_id].attempts = run.attempt_offsets[node_id]
            if step["state"] == "succeeded":
                output = step.get("output") or None
                run.data[node_id] = output
                run.node_runs[node_id].output = output
        for event in events:
            if event["type"] != "node-finished":
                continue
            node_id = str(event["payload"].get("node_id") or "")
            nr = run.node_runs.get(node_id)
            if nr is None:
                continue
            try:
                nr.state = NodeState(str(event["payload"].get("state") or "unknown"))
            except ValueError:
                nr.state = NodeState.UNKNOWN
            nr.attempts = int(event["payload"].get("attempts") or nr.attempts)
        inflight: list[str] = []
        for node_id in run.attempt_offsets:
            nr = run.node_runs.get(node_id)
            if nr is not None and nr.state is NodeState.PENDING:
                nr.state = NodeState.UNKNOWN  # 有尝试痕迹却无终态事件 → 崩溃在途
                inflight.append(node_id)
        return run, inflight

    async def _recover_later(self) -> None:
        """延迟恢复（§5.15 ``resume_delay``）：容忍心跳注册表空窗，不阻塞启动。"""
        await asyncio.sleep(self.config.resume_delay)
        await self._recover()

    async def _recover(self) -> None:
        """恢复扫描（§5.10）：全部在途幂等 → 重放；任一未知 → interrupted。"""
        assert self._store is not None
        rows = await self._store.unfinished_runs()
        for row in rows:
            run_id = str(row["id"])
            try:
                rebuilt = await self._rebuild_run(run_id)
                if rebuilt is None:
                    continue
                run, crashed = rebuilt
                unknown: str | None = None
                for node_id in crashed:
                    binding = (row.get("bindings") or {}).get(node_id) or {}
                    # 幂等性取自崩溃那次尝试的绑定快照（提供者此刻可能还没注册）
                    idempotent = binding.get("idempotent")
                    if idempotent is None:  # 兼容无快照的旧数据：查当前注册表
                        record = self._find_record(
                            str(binding.get("provider") or ""),
                            str(binding.get("step") or ""),
                        )
                        idempotent = bool(record is not None and record.idempotent)
                    if not idempotent:
                        unknown = node_id
                        break
                if unknown is not None:
                    # 非幂等在途：不臆断结果 → unknown + interrupted（人工处置）
                    await self._store.record_run_interrupted(
                        run_id, node_id=unknown,
                        reason="crash_recovered_unknown_attempt",
                    )
                    self._stats["runs_interrupted"] += 1
                    await self._dispatch(
                        EVT_RUN_INTERRUPTED,
                        {
                            "run_id": run_id, "node_id": unknown,
                            "reason": "crash_recovered_unknown_attempt",
                        },
                    )
                    continue
                for node_id in crashed:
                    run.node_runs[node_id].state = NodeState.PENDING  # 幂等步重放
                self._runs[run_id] = run
                self._tasks[run_id] = asyncio.create_task(self._run(run))
            except Exception as exc:  # noqa: BLE001 —— 单 run 恢复失败不拖垮启动
                self._provided_ctx.logger.error("flow 恢复 %s 失败：%s", run_id, exc)

    async def _retain_once(self) -> None:
        """保留双限清理（§5.10）：失败只记日志，不影响主链路。"""
        if self._store is None:
            return
        try:
            removed = await self._store.retain(
                retain_days=self.config.retain_days,
                max_runs=self.config.retain_max_runs,
            )
            if removed:
                self._provided_ctx.logger.info("flow 保留清理：%d 个过期 run", removed)
        except Exception as exc:  # noqa: BLE001
            self._provided_ctx.logger.error("flow 保留清理失败：%s", exc)

    # -------------------------------------------- 用户流程内部（§5.14）
    def _resolve_flow(self, flow_id: str) -> tuple[FlowDef, Overrides | None, int]:
        """解析流程（§5.14.1 三层）：模板优先，其次用户流程；返回 (基底, 持久化覆盖, 版本)。"""
        entry = self._user_flows.get(flow_id)
        if entry is not None:
            row = entry["row"]
            if not row["enabled"]:
                raise FlowError(f"流程已停用：{flow_id!r}")
            if row["needs_attention"]:
                raise FlowError(f"流程需重新校验（模板已升级）：{flow_id!r}")
            return entry["flow"], entry["overrides"], int(row["version"])
        flow = self._registry.flow(flow_id)
        if flow is None:
            raise FlowError(f"流程未注册：{flow_id!r}")
        return flow, None, flow.version

    def _effective_nodes(
        self, flow: FlowDef, overrides: Overrides | None
    ) -> list[dict[str, Any]]:
        """生效节点（详情 / 编辑器用；含 ``disabled`` 标记）。"""
        nodes, disabled, _problems = apply_overrides(flow, overrides)
        items: list[dict[str, Any]] = []
        for node in nodes:
            entry = dict(flow_def_to_json(FlowDef(id="x", version=1, nodes=(node,)))["nodes"][0])  # type: ignore[index]
            entry["disabled"] = node.id in disabled
            items.append(entry)
        return items

    async def _load_user_flows(self) -> None:
        """启动装载：重建缓存 + 全量重校验（模板可能在停机期间升级）。"""
        await self._revalidate_user_flows()

    async def _revalidate_user_flows(self, template_id: str | None = None) -> None:
        """重建用户流程缓存并按当前模板重校验（§5.14.1：升级即校验）。

        语义 / 参数错 → ``needs_attention=1``（禁止新启动，已在跑的 run 不受
        影响）；**缺提供者不算错**（§5.14.5）。
        """
        if self._store is None:
            return
        rows = await self._store.defs()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:  # 新 → 旧
            latest.setdefault(str(row["id"]), row)
        rebuilt: dict[str, dict[str, Any]] = {}
        for flow_id, row in latest.items():
            entry = self._build_entry(row)
            rebuilt[flow_id] = entry
            if template_id is not None and entry["extends"] != template_id:
                continue
            flag = 1 if entry["problems"] else 0
            if bool(row["needs_attention"]) != bool(flag):
                await self._store.set_def_flags(
                    flow_id, int(row["version"]), needs_attention=flag
                )
                row["needs_attention"] = bool(flag)
                entry["row"]["needs_attention"] = bool(flag)  # row 是副本，两处都记
        self._user_flows = rebuilt

    def _build_entry(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """缓存条目：基底流程 + 持久化覆盖 + 语义问题（needs_attention 判据）。"""
        body = row["body"] if isinstance(row.get("body"), dict) else {}
        extends = str(row.get("extends") or "")
        problems: list[Problem] = []
        overrides: Overrides | None = None
        if extends:
            template = self._registry.flow(extends)
            if template is None:
                problems.append(
                    Problem(
                        ProblemLevel.ERROR, extends, ProblemCode.UNKNOWN_NODE,
                        f"模板不存在：{extends!r}",
                    )
                )
                flow = FlowDef(id=extends, version=0, nodes=())
            else:
                flow = template
                overrides = dict(body.get("overrides") or {})
                _nodes, _disabled, semantic = apply_overrides(flow, overrides)
                problems += [p for p in semantic if p.level is ProblemLevel.ERROR]
                problems += [
                    p
                    for p in preflight_flow(flow, overrides, self._registry.records())
                    if p.code is ProblemCode.BAD_PARAMS
                ]
        else:
            try:
                flow = parse_flow_def(dict(body.get("def") or {}))
            except (TypeError, ValueError) as exc:
                problems.append(
                    Problem(
                        ProblemLevel.ERROR, str(row.get("id") or ""),
                        ProblemCode.UNKNOWN_NODE, f"定义不合法：{exc}",
                    )
                )
                flow = FlowDef(id=str(row.get("id") or ""), version=0, nodes=())
        return {
            "row": dict(row), "flow": flow, "overrides": overrides,
            "extends": extends, "problems": problems,
        }

    def _audit(self, action: str, target: str, actor: str, detail: str = "") -> None:
        """审计（§5.14.6：谁、何时、什么）——defs 的 created_by + 日志留痕。"""
        self._provided_ctx.logger.info(
            "[flow-audit] %s target=%s actor=%s%s",
            action, target, actor or "anonymous", f" {detail}" if detail else "",
        )

    def _on_steps_changed(
        self, stage: str, step_id: str, action: str, provider: str
    ) -> None:
        """注册变更 → ``flow/steps-changed``（§5.8.6：经 parallel，出错隔离）。"""
        task = asyncio.create_task(
            self._dispatch(
                EVT_STEPS_CHANGED,
                {"stage": stage, "step_id": step_id, "action": action, "provider": provider},
            )
        )
        task.add_done_callback(self._log_task_exit)

    async def _dispatch(self, name: str, payload: dict[str, object]) -> None:
        errors = await parallel_event(self._provided_ctx, name, payload)
        for error in errors:
            self._provided_ctx.logger.error("flow 事件 %s 监听器失败：%s", name, error)

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """后台任务观察哨：意外退出记 error 日志，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._provided_ctx.logger.error("flow 后台任务意外退出：%s", error)

    def _caller_fiber(self) -> Fiber:
        """取调用方 fiber（影子上下文语义，与 webapi 相同）。"""
        fiber = getattr(self.ctx, "fiber", None)
        return fiber if fiber is not None else self._provided_ctx.fiber


__all__: list[str] = ["FlowService"]
