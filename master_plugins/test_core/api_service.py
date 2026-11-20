"""test-core 管理 API 服务（``ctx.test_core_api``）。

核心（TestCorePlugin）承载状态机 / 编排 / 落库，不沾 Web 依赖；管理 API 独立
成服务、``inject = ["test_core", "webapi"]`` 注入式挂载（同 heartbeat-server
模式）：两者就位 cordis 才装载并挂路由，缺席安静 PENDING，核心 / webapi 换装、
重载随依赖纪元自动重挂。HTTP 参数解析与可扩展返回值（``extensions`` 收集槽）
留在本层，数据操作一律走核心公共门面 ``ctx.test_core``
（:class:`~common.test_contracts.TestCoreInterface`），禁止直连 db。
路由表见 DESIGN.md §12。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NamedTuple, cast

from cordis_port import Context, EventDispatchError, Service
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from common.test_contracts import (
    EVENT_COLLECT_TASK,
    DuplicateSubmission,
    ExecutorSelector,
    InvalidSpec,
    InvalidTransition,
    MaterialInUse,
    MaterialNotFound,
    PlanNotFound,
    ShardSpec,
    StageSpec,
    TaskNotFound,
    TaskSpec,
)

if TYPE_CHECKING:  # 运行期无环：plugin 导入本模块装载子服务
    from .plugin import TestCorePlugin

#: 任务状态字面量全集（列表过滤下拉的机器口径，与 TaskState 契约一致）
TASK_STATES: tuple[str, ...] = (
    "pending",
    "planning",
    "running",
    "canceling",
    "succeeded",
    "failed",
    "canceled",
    "error",
)


# -- 错误边界（统一异常映射，DESIGN §12） -------------------------------------


class _BodyTooLarge(Exception):
    """请求体超限（材料上传映射 413）。"""


def _guarded(
    fn: Callable[..., dict[str, object] | JSONResponse],
) -> Callable[..., dict[str, object] | JSONResponse]:
    """把 handler 的异常映射为错误响应：``{"ok": false, "error", "code"}``。

    映射表：TaskNotFound→404、MaterialNotFound→404、PlanNotFound→404
    （``plan_not_found``）、InvalidSpec→400、InvalidTransition→409、
    DuplicateSubmission→409、MaterialInUse→409、请求体超限（_BodyTooLarge）
    →413、JSON 解析 / 参数校验（ValueError）→400、其余→500（不泄漏堆栈，
    只记 ``ctx.logger``）。
    """

    @wraps(fn)
    async def wrapper(
        self: TestCoreApiService, request: Request, *args: object, **kwargs: object
    ) -> dict[str, object] | JSONResponse:
        try:
            return await fn(self, request, *args, **kwargs)
        except TaskNotFound as exc:
            return _error_response(404, str(exc) or "任务不存在", "not_found")
        except MaterialNotFound as exc:
            return _error_response(404, str(exc) or "材料 / 选择集不存在", "not_found")
        except PlanNotFound as exc:
            return _error_response(404, str(exc) or "测试计划不存在", "plan_not_found")
        except InvalidSpec as exc:
            return _error_response(400, str(exc) or "任务规格无效", "invalid_spec")
        except InvalidTransition as exc:
            return _error_response(409, str(exc) or "非法状态迁移", "invalid_transition")
        except DuplicateSubmission as exc:
            return _error_response(409, str(exc) or "重复提交", "duplicate_submission")
        except MaterialInUse as exc:
            return _error_response(409, str(exc) or "材料被引用，禁止删除", "material_in_use")
        except _BodyTooLarge as exc:
            return _error_response(413, str(exc) or "请求体超限", "payload_too_large")
        except json.JSONDecodeError as exc:
            return _error_response(400, f"请求体不是合法 JSON：{exc}", "invalid_json")
        except ValueError as exc:
            return _error_response(400, str(exc) or "参数不合法", "bad_request")
        except Exception as exc:  # noqa: BLE001 —— 兜底 500，禁止泄漏堆栈
            self.ctx.logger.error("test-core 管理接口 %s 意外失败：%s", fn.__name__, exc)
            return _error_response(500, "服务器内部错误", "internal_error")

    return wrapper


class TestCoreApiService(Service[None]):
    """test-core 管理 API 服务：挂任务提交 / 查询 / 取消 / 重跑等接口。"""

    #: 附属服务标记：随宿主 ``test-core`` 一起呈现，不单列插件
    service_of: ClassVar[str] = "test-core"
    #: 服务名（即 ``ctx.test_core_api``）
    provide: str | None = "test_core_api"
    #: fiber 名（路由归属与诊断用）
    name: str = "test-core_api"
    #: 依赖集 = 自家核心 ``test_core`` + Web 服务 ``webapi``
    inject: ClassVar[list[str]] = ["test_core", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.test_core_api
        self.ctx: Context = ctx

    async def init(self) -> object:
        """挂管理接口与健康指标（依赖集由 fiber 保证就绪，无需轮询）。

        路由注册绑定调用方 fiber：本服务卸载 / 重载时自动撤销重挂；健康指标
        句柄作为清理函数返回，随 fiber 卸载注销。
        """
        api = self.ctx.webapi
        api.post("/api/test/tasks", self._task_submit, kind="api", name="test-core.task-submit")
        api.get("/api/test/tasks", self._task_list, kind="api", name="test-core.tasks")
        api.get("/api/test/tasks/{task_id}", self._task_detail, kind="api", name="test-core.task")
        api.get(
            "/api/test/tasks/{task_id}/jobs",
            self._task_jobs,
            kind="api",
            name="test-core.task-jobs",
        )
        api.get(
            "/api/test/tasks/{task_id}/result",
            self._task_result,
            kind="api",
            name="test-core.task-result",
        )
        api.post(
            "/api/test/tasks/{task_id}/cancel",
            self._task_cancel,
            kind="api",
            name="test-core.task-cancel",
        )
        api.post(
            "/api/test/tasks/{task_id}/rerun",
            self._task_rerun,
            kind="api",
            name="test-core.task-rerun",
        )
        api.patch(
            "/api/test/tasks/{task_id}",
            self._task_patch,
            kind="api",
            name="test-core.task-update",
        )
        api.get("/api/test/cases", self._cases, kind="api", name="test-core.cases")
        api.get("/api/test/artifacts", self._artifacts, kind="api", name="test-core.artifacts")
        api.get(
            "/api/test/artifacts/{artifact_id}/download",
            self._artifact_download,
            kind="api",
            name="test-core.artifact-download",
        )
        api.get(
            "/api/test/jobs/{job_id}/logs",
            self._job_logs,
            kind="api",
            name="test-core.job-logs",
        )
        api.get("/api/test/plans", self._plan_list, kind="api", name="test-core.plans")
        api.post("/api/test/plans", self._plan_create, kind="api", name="test-core.plan-create")
        api.get("/api/test/plans/{plan_id}", self._plan_detail, kind="api", name="test-core.plan")
        api.put(
            "/api/test/plans/{plan_id}",
            self._plan_update,
            kind="api",
            name="test-core.plan-update",
        )
        api.delete(
            "/api/test/plans/{plan_id}",
            self._plan_delete,
            kind="api",
            name="test-core.plan-delete",
        )
        api.post(
            "/api/test/plans/{plan_id}/clone",
            self._plan_clone,
            kind="api",
            name="test-core.plan-clone",
        )
        api.post(
            "/api/test/plans/{plan_id}/dispatch",
            self._plan_dispatch,
            kind="api",
            name="test-core.plan-dispatch",
        )
        api.post(
            "/api/test/materials",
            self._material_upload,
            kind="api",
            name="test-core.material-upload",
        )
        api.get("/api/test/materials", self._material_list, kind="api", name="test-core.materials")
        api.get(
            "/api/test/materials/{material_id}",
            self._material_detail,
            kind="api",
            name="test-core.material",
        )
        api.delete(
            "/api/test/materials/{material_id}",
            self._material_delete,
            kind="api",
            name="test-core.material-delete",
        )
        api.get(
            "/api/test/materials/{material_id}/cases",
            self._material_cases,
            kind="api",
            name="test-core.material-cases",
        )
        api.get(
            "/api/test/materials/{material_id}/download",
            self._material_download,
            kind="api",
            name="test-core.material-download",
        )
        api.get(
            "/api/test/selections/{selection_id}",
            self._selection_detail,
            kind="api",
            name="test-core.selection",
        )
        return api.add_health_provider("test_core", self._core.health_metrics)

    @property
    def _core(self) -> TestCorePlugin:
        return cast("TestCorePlugin", self.ctx.get("test_core"))

    # -- 管理 API（设计稿 §12） -------------------------------------------------
    @_guarded
    async def _task_submit(self, request: Request) -> dict[str, object] | JSONResponse:
        """提交任务：body = TaskSpec JSON；``Idempotency-Key`` 头可选。"""
        data = _required_json_object(await request.body())
        try:
            spec = spec_from_dict(data)
        except ValueError as exc:  # 规格类型 / 缺字段问题统一 invalid_spec（DESIGN §12）
            raise InvalidSpec(str(exc)) from exc
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if idempotency_key:  # 头显式给出时以头为准（CI 重复触发的请求级去重键）
            spec = replace(spec, idempotency_key=idempotency_key)
        task_id = await self._core.submit(spec, source="api")
        return {"ok": True, "task_id": task_id}

    @_guarded
    async def _task_list(self, request: Request) -> dict[str, object] | JSONResponse:
        """任务列表：query ``state`` / ``labels``（JSON 或 k=v 逗号分隔）/ ``limit`` / ``offset``。"""
        params = request.query_params
        state = params.get("state", "").strip() or None
        labels = _parse_labels(params.get("labels", ""))
        limit = _parse_int(params.get("limit"), default=50, minimum=1, name="limit")
        offset = _parse_int(params.get("offset"), default=0, minimum=0, name="offset")
        tasks = await self._core.list_tasks(
            state=state, labels=labels, limit=limit, offset=offset
        )
        return {
            "ok": True,
            "tasks": list(tasks),
            "count": len(tasks),
            "limit": limit,
            "offset": offset,
        }

    @_guarded
    async def _task_detail(
        self, request: Request, task_id: str
    ) -> dict[str, object] | JSONResponse:
        """任务详情：``task + stages + history`` + ``extensions`` 收集槽（README 形态）。

        ``test/collect-task`` 由核心 ``get_task`` 统一派发一次，本层沿用其收集
        结果组装 HTTP 形态、不二次派发（否则消费方监听器在一次详情请求里跑两遍）；
        核心未带收集槽时才在本层兜底收集，保证 ``extensions`` 恒在。
        """
        detail = await self._core.get_task(task_id)
        if detail is None:
            raise TaskNotFound(f"找不到任务：{task_id}")
        response: dict[str, object] = {
            "ok": True,
            "task": _task_section(detail),
            "stages": list(detail.get("stages") or []),
            "history": await self._task_history(detail, task_id),
        }
        if "extensions" in detail:
            response["extensions"] = dict(detail.get("extensions") or {})
            response["extension_errors"] = list(detail.get("extension_errors") or [])
        else:  # 兜底：核心未收集时在 HTTP 层收集（同 heartbeat collect-node 模式）
            await self._extend(response, EVENT_COLLECT_TASK)
        return response

    async def _task_history(
        self, detail: Mapping[str, Any], task_id: str
    ) -> list[dict[str, Any]]:
        """状态迁移时间线（管理页 Timeline）：门面已带则用，否则经核心仓储补查。

        ``TestCoreInterface`` 暂无 task_history 只读口（契约层缺口，见汇报
        「待裁决」）；这里经核心自己的仓储访问器读表，不绕过核心直连 db。
        """
        history = detail.get("history")
        if isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
            return [dict(row) for row in history if isinstance(row, Mapping)]
        rows = await self._core.list_task_history(task_id)
        return [dict(row) for row in rows]

    @_guarded
    async def _task_jobs(
        self, request: Request, task_id: str
    ) -> dict[str, object] | JSONResponse:
        """任务的 Job 列表（含 attempt 历史）；任务不存在 → 404。"""
        # 判存用 get_result（不触发 collect-task 派发），与 …/result 同口径 404
        if await self._core.get_result(task_id) is None:
            raise TaskNotFound(f"找不到任务：{task_id}")
        jobs = await self._core.list_jobs(task_id)
        return {"ok": True, "task_id": task_id, "jobs": list(jobs), "count": len(jobs)}

    @_guarded
    async def _task_result(
        self, request: Request, task_id: str
    ) -> dict[str, object] | JSONResponse:
        """聚合结果 + 各 Job/attempt 明细引用。"""
        result = await self._core.get_result(task_id)
        if result is None:
            raise TaskNotFound(f"找不到任务：{task_id}")
        return {"ok": True, "task_id": task_id, "result": dict(result)}

    @_guarded
    async def _task_cancel(
        self, request: Request, task_id: str
    ) -> dict[str, object] | JSONResponse:
        """请求取消；body 可带 ``{"reason": "..."}``。"""
        body = _optional_json_object(await request.body())
        reason = body.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError("reason 需是字符串")
        canceled = await self._core.cancel(task_id, reason)
        if not canceled:
            if await self._core.get_task(task_id) is None:
                raise TaskNotFound(f"找不到任务：{task_id}")
            raise InvalidTransition(f"任务已终结，不可取消：{task_id}")
        return {"ok": True, "task_id": task_id, "canceled": True}

    @_guarded
    async def _task_rerun(
        self, request: Request, task_id: str
    ) -> dict[str, object] | JSONResponse:
        """重跑失败 Job（同 Task 内 attempt+1）。"""
        returned = await self._core.rerun_failed(task_id)
        return {"ok": True, "task_id": returned or task_id}

    @_guarded
    async def _task_patch(
        self, request: Request, task_id: str
    ) -> dict[str, object] | JSONResponse:
        """改优先级：``{"priority": N}``。"""
        body = _required_json_object(await request.body())
        if "priority" not in body:
            raise ValueError("缺少 priority 字段")
        priority = body["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError(f"priority 需是整数，收到 {priority!r}")
        updated = await self._core.set_priority(task_id, priority)
        if not updated and await self._core.get_task(task_id) is None:
            raise TaskNotFound(f"找不到任务：{task_id}")
        return {
            "ok": True,
            "task_id": task_id,
            "priority": priority,
            "updated": bool(updated),
        }

    @_guarded
    async def _cases(self, request: Request) -> dict[str, object] | JSONResponse:
        """用例结果查询：query ``task_id`` / ``job_id`` / ``status`` / ``limit``。"""
        params = request.query_params
        limit = _parse_int(params.get("limit"), default=200, minimum=1, name="limit")
        cases = await self._core.list_case_results(
            task_id=params.get("task_id", "").strip(),
            job_id=params.get("job_id", "").strip(),
            status=params.get("status", "").strip(),
            limit=limit,
        )
        return {"ok": True, "cases": list(cases), "count": len(cases), "limit": limit}

    @_guarded
    async def _artifacts(self, request: Request) -> dict[str, object] | JSONResponse:
        """产物列表：query ``task_id`` / ``state``（缺省不过滤状态）。"""
        params = request.query_params
        artifacts = await self._core.list_artifacts(
            task_id=params.get("task_id", "").strip(),
            state=params.get("state", "").strip(),
        )
        return {"ok": True, "artifacts": list(artifacts), "count": len(artifacts)}

    @_guarded
    async def _artifact_download(
        self, request: Request, artifact_id: str
    ) -> Response:
        """本地产物下载：``local://`` 回文件；外部 URI（oss:// 等）302；找不到 404。"""
        artifact = await self._find_artifact(artifact_id)
        if artifact is None:
            return _error_response(404, f"找不到产物：{artifact_id}", "not_found")
        uri = str(artifact.get("uri") or "")
        if uri.startswith("local://"):
            path = _local_uri_path(uri)
            if not path.is_file():
                return _error_response(404, f"产物文件不存在：{artifact_id}", "not_found")
            name = str(artifact.get("name") or path.name)
            return FileResponse(path, filename=name)
        if "://" in uri:  # 已转存外部（artifact_oss 回填 oss:// 等）
            return RedirectResponse(uri, status_code=302)
        return _error_response(404, f"产物 URI 不可下载：{artifact_id}", "not_found")

    async def _find_artifact(self, artifact_id: str) -> Mapping[str, Any] | None:
        """按 id 找产物引用（门面只有列表口，逐状态收敛查询）。"""
        for state in ("", "local", "uploaded"):
            for item in await self._core.list_artifacts(state=state):
                if str(item.get("artifact_id") or item.get("id") or "") == artifact_id:
                    return item
        return None

    # -- 测试计划 API（§4.4 / §6）与 Job 日志端点 ------------------------------
    @_guarded
    async def _plan_list(self, request: Request) -> dict[str, object] | JSONResponse:
        """计划列表：query ``keyword`` / ``labels``（JSON 或 k=v 逗号分隔）/ ``limit`` / ``offset``。"""
        params = request.query_params
        plans = await self._core.list_plans(
            keyword=params.get("keyword", "").strip(),
            labels=_parse_labels(params.get("labels", "")),
            limit=_parse_int(params.get("limit"), default=50, minimum=1, name="limit"),
            offset=_parse_int(params.get("offset"), default=0, minimum=0, name="offset"),
        )
        return {"ok": True, "plans": list(plans), "count": len(plans)}

    @_guarded
    async def _plan_create(self, request: Request) -> dict[str, object] | JSONResponse:
        """新建计划：body = name / description / labels / defaults / steps（§4.1）。"""
        fields = _plan_body(_required_json_object(await request.body()))
        plan = await self._core.save_plan(
            name=fields.name,
            description=fields.description,
            labels=fields.labels,
            defaults=fields.defaults,
            steps=fields.steps,
            created_by=fields.created_by,
        )
        return {"ok": True, "plan": dict(plan)}

    @_guarded
    async def _plan_detail(
        self, request: Request, plan_id: str
    ) -> dict[str, object] | JSONResponse:
        """计划详情；缺失 404（``plan_not_found``）。"""
        plan = await self._core.get_plan(plan_id)
        if plan is None:
            raise PlanNotFound(f"找不到测试计划：{plan_id}")
        return {"ok": True, "plan": dict(plan)}

    @_guarded
    async def _plan_update(
        self, request: Request, plan_id: str
    ) -> dict[str, object] | JSONResponse:
        """更新计划（save_plan 全量落字段）；计划不存在由 save_plan 抛 PlanNotFound → 404。"""
        fields = _plan_body(_required_json_object(await request.body()))
        plan = await self._core.save_plan(
            plan_id=plan_id,
            name=fields.name,
            description=fields.description,
            labels=fields.labels,
            defaults=fields.defaults,
            steps=fields.steps,
            created_by=fields.created_by,
        )
        return {"ok": True, "plan": dict(plan)}

    @_guarded
    async def _plan_delete(
        self, request: Request, plan_id: str
    ) -> dict[str, object] | JSONResponse:
        """删除计划：缺失 404（``plan_not_found``）。"""
        if not await self._core.delete_plan(plan_id):
            raise PlanNotFound(f"找不到测试计划：{plan_id}")
        return {"ok": True, "deleted": True}

    @_guarded
    async def _plan_clone(
        self, request: Request, plan_id: str
    ) -> dict[str, object] | JSONResponse:
        """另存为（get + save 组合）：name 加「副本」后缀；created_by 取 query / body（可选）。"""
        source = await self._core.get_plan(plan_id)
        if source is None:
            raise PlanNotFound(f"找不到测试计划：{plan_id}")
        body = _optional_json_object(await request.body())
        created_by = request.query_params.get("created_by", "").strip()
        if not created_by:
            created_by = _as_str(body.get("created_by", ""), "created_by")
        plan = await self._core.save_plan(
            name=f"{source.get('name') or plan_id} 副本",
            description=str(source.get("description") or ""),
            labels=source.get("labels"),
            defaults=source.get("defaults"),
            steps=source.get("steps"),
            created_by=created_by,
        )
        return {"ok": True, "plan": dict(plan)}

    @_guarded
    async def _plan_dispatch(
        self, request: Request, plan_id: str
    ) -> dict[str, object] | JSONResponse:
        """下发计划：body = overrides（可空）；``Idempotency-Key`` 头优先于 body.idempotency_key。

        幂等域 ``source="plan-dispatch"``（评审 #13）在核心 dispatch_plan 内隔离；
        overrides 未知键由核心抛 InvalidSpec → 400。
        """
        overrides = dict(_optional_json_object(await request.body()))
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if idempotency_key:  # 头显式给出时以头为准（既有惯例，同任务提交）
            overrides["idempotency_key"] = idempotency_key
        result = await self._core.dispatch_plan(plan_id, overrides or None)
        return {
            "ok": True,
            "task_id": str(result.get("task_id") or ""),
            "deduplicated": bool(result.get("deduplicated")),
        }

    @_guarded
    async def _job_logs(
        self, request: Request, job_id: str
    ) -> dict[str, object] | JSONResponse:
        """Job 日志游标端点（§6）：``ctx.log.query_logs(name_prefix="test.{job_id}.")``。

        恒返回旧 → 新；``since_id`` > 0 才下传 after_id 游标；``limit`` 钳到
        [1, 1000]；``next_since_id`` = 本批最大 id（空批保持原游标不回退）；
        log 插件缺席降级 ``log_available: false``。
        """
        params = request.query_params
        since_id = _parse_int(params.get("since_id"), default=0, minimum=0, name="since_id")
        limit = _parse_int(params.get("limit"), default=200, minimum=0, name="limit")
        log = self.ctx.get("log")
        if log is None:
            return {"ok": True, "logs": [], "log_available": False}
        rows = await log.query_logs(
            name_prefix=f"test.{job_id}.",
            after_id=since_id if since_id > 0 else None,
            limit=min(max(limit, 1), 1000),
        )
        ordered = sorted(rows, key=_row_id)  # 恒旧 → 新（尾随序）
        max_id = max((_row_id(row) for row in ordered), default=0)
        return {
            "ok": True,
            "logs": ordered,
            "log_available": True,
            "next_since_id": max(max_id, since_id),
        }

    # -- 材料 / 选择集 API（executor_pytest DESIGN §18.5） ---------------------
    @_guarded
    async def _material_upload(
        self, request: Request
    ) -> dict[str, object] | JSONResponse:
        """上传材料 zip：二进制 body（上限 config.material_max_bytes，超限 413）。

        ``X-Material-Name`` 头（或 query ``name``）必填；``X-Material-Recipe`` 头
        （或 query ``recipe``）为执行器自定义不透明字符串（core 不解析、原样透传，
        缺省空串；含非 Latin-1 字符时同 X-Material-Name 的降级模式改走 query
        ``recipe`` 参数）；``X-Material-Executor`` 头（可选）绑定执行器归属
        （空 = 通用材料，§4.5）。``(sha256, recipe_fingerprint)`` 命中返回既有
        材料（``deduplicated`` = true，幂等，§4.3）。
        """
        params = request.query_params
        name = (
            request.headers.get("X-Material-Name", "").strip()
            or params.get("name", "").strip()
        )
        if not name:
            raise ValueError("缺少材料名（X-Material-Name 头或 name 查询参数）")
        recipe = request.headers.get("X-Material-Recipe", "") or params.get("recipe", "")
        data = await _read_body_capped(
            request, limit=self._core.config.material_max_bytes
        )
        material = await self._core.upload_material(
            name=name,
            data=data,
            recipe=recipe,
            executor_id=request.headers.get("X-Material-Executor", "").strip(),
        )
        return {
            "ok": True,
            "material": dict(material),
            "deduplicated": bool(material.get("deduplicated")),
        }

    @_guarded
    async def _material_list(
        self, request: Request
    ) -> dict[str, object] | JSONResponse:
        """材料列表：query ``executor`` / ``state`` / ``labels``（JSON 或 k=v 逗号分隔）
        / ``limit`` / ``offset``。``executor`` 过滤谓词 ``executor_id == X OR = ''``
        （通用可见）由服务面实现（§4.5）。
        """
        params = request.query_params
        limit = _parse_int(params.get("limit"), default=50, minimum=1, name="limit")
        offset = _parse_int(params.get("offset"), default=0, minimum=0, name="offset")
        materials = await self._core.list_materials(
            state=params.get("state", "").strip(),
            executor_id=params.get("executor", "").strip(),
            labels=_parse_labels(params.get("labels", "")),
            limit=limit,
            offset=offset,
        )
        return {
            "ok": True,
            "materials": list(materials),
            "count": len(materials),
            "limit": limit,
            "offset": offset,
        }

    @_guarded
    async def _material_detail(
        self, request: Request, material_id: str
    ) -> dict[str, object] | JSONResponse:
        """材料详情（含 recipe / problems）；不存在 404。"""
        material = await self._core.get_material(material_id)
        if material is None:
            raise MaterialNotFound(f"找不到材料：{material_id}")
        return {"ok": True, "material": dict(material)}

    @_guarded
    async def _material_delete(
        self, request: Request, material_id: str
    ) -> dict[str, object] | JSONResponse:
        """删除材料：有引用 409（material_in_use）、不存在 404（§3.2）。"""
        await self._core.delete_material(material_id)
        return {"ok": True, "material_id": material_id, "deleted": True}

    @_guarded
    async def _material_cases(
        self, request: Request, material_id: str
    ) -> dict[str, object] | JSONResponse:
        """材料用例清单（转调核心 material_case 查询）；材料不存在 404。"""
        if await self._core.get_material(material_id) is None:
            raise MaterialNotFound(f"找不到材料：{material_id}")
        cases = await self._core.list_material_cases(material_id)
        return {
            "ok": True,
            "material_id": material_id,
            "cases": list(cases),
            "count": len(cases),
        }

    @_guarded
    async def _material_download(self, request: Request, material_id: str) -> Response:
        """材料 zip 本体下载：FileResponse 回文件；材料或文件缺失 404。"""
        material = await self._core.get_material(material_id)
        if material is None:
            return _error_response(404, f"找不到材料：{material_id}", "not_found")
        uri = str(material.get("uri") or "")
        path = _local_uri_path(uri) if uri.startswith("local://") else Path()
        if not path.is_file():
            return _error_response(404, f"材料文件不存在：{material_id}", "not_found")
        return FileResponse(
            path, filename=f"{material_id}.zip", media_type="application/zip"
        )

    @_guarded
    async def _selection_detail(
        self, request: Request, selection_id: str
    ) -> dict[str, object] | JSONResponse:
        """选择集详情；不存在 404。"""
        selection = await self._core.get_selection(selection_id)
        if selection is None:
            raise MaterialNotFound(f"找不到选择集：{selection_id}")
        return {"ok": True, "selection": dict(selection)}

    async def _extend(self, response: dict[str, object], event: str) -> None:
        """可扩展返回值契约（同 heartbeat ``collect-node`` 模式，DESIGN §11.3）。

        响应构造好后并发派发 ``event``，监听器把自家数据写进
        ``response["extensions"]["<插件名>"]``；某个贡献者出错不影响接口 ——
        错误进 ``response["extension_errors"]``（恒在），其余贡献照常返回。
        """
        extensions: dict[str, object] = {}
        errors: list[str] = []
        response["extensions"] = extensions
        response["extension_errors"] = errors
        try:
            await self.ctx.parallel(event, response)
        except EventDispatchError as exc:
            errors.extend(f"{type(error).__name__}: {error}" for error in exc.errors)


# -- TaskSpec 解析（模块级公共 helper，测试可直接引用） -----------------------


def spec_from_dict(data: Mapping[str, Any]) -> TaskSpec:
    """把提交 body 解析成 :class:`TaskSpec`（递归解析 stages / executor / shard）。

    容错规则（同契约兼容规则）：未知字段忽略、缺省取 dataclass 默认值；
    必填字段缺失或类型明显不对抛 :class:`ValueError`（HTTP 层映射 400）。
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"任务规格需是对象，收到 {type(data).__name__}")
    name = _as_str(_require(data, "name"), "name", allow_empty=False)
    stages_raw = _require(data, "stages")
    if isinstance(stages_raw, (str, bytes)) or not isinstance(stages_raw, Sequence):
        raise ValueError(f"stages 需是数组，收到 {type(stages_raw).__name__}")
    stages = tuple(
        _stage_from(item, f"stages[{index}]") for index, item in enumerate(stages_raw)
    )
    priority = _as_int(data.get("priority", 0), "priority")
    labels = _as_str_map(data.get("labels", {}), "labels")
    params = _as_any_map(data.get("params", {}), "params")
    idempotency_key = _as_str(data.get("idempotency_key", ""), "idempotency_key")
    failure_policy = _as_str(
        data.get("failure_policy", "fail_fast"), "failure_policy", allow_empty=False
    )
    if failure_policy not in ("fail_fast", "continue"):
        raise ValueError(
            f"failure_policy 只能是 fail_fast / continue，收到 {failure_policy!r}"
        )
    return TaskSpec(
        name=name,
        stages=stages,
        priority=priority,
        labels=labels,
        params=params,
        idempotency_key=idempotency_key,
        failure_policy=cast(Literal["fail_fast", "continue"], failure_policy),
        timeout_s=_as_seconds(data.get("timeout_s", 0.0), "timeout_s"),
    )


def _executor_from(value: object, path: str) -> ExecutorSelector:
    data = _as_mapping(value, path)
    return ExecutorSelector(
        id=_as_str(_require(data, "id", path), f"{path}.id", allow_empty=False),
        kind=_as_str(data.get("kind", "executor"), f"{path}.kind", allow_empty=False),
        labels=_as_str_map(data.get("labels", {}), f"{path}.labels"),
    )


def _shard_from(value: object, path: str) -> ShardSpec:
    data = _as_mapping(value, path)
    mode = _as_str(data.get("mode", "none"), f"{path}.mode", allow_empty=False)
    if mode not in ("none", "plan"):
        raise ValueError(f"{path}.mode 只能是 none / plan，收到 {mode!r}")
    hint = _as_int(data.get("hint", 0), f"{path}.hint")
    if hint < 0:
        raise ValueError(f"{path}.hint 需 ≥ 0，收到 {hint}")
    return ShardSpec(mode=cast(Literal["none", "plan"], mode), hint=hint)


def _stage_from(value: object, path: str) -> StageSpec:
    data = _as_mapping(value, path)
    return StageSpec(
        name=_as_str(_require(data, "name", path), f"{path}.name", allow_empty=False),
        executor=_executor_from(_require(data, "executor", path), f"{path}.executor"),
        params=_as_any_map(_require(data, "params", path), f"{path}.params"),
        shard=_shard_from(data.get("shard", {}), f"{path}.shard"),
        requires=_as_int_map(data.get("requires", {}), f"{path}.requires"),
        timeout_s=_as_seconds(data.get("timeout_s", 0.0), f"{path}.timeout_s"),
    )


def _require(data: Mapping[str, Any], key: str, path: str = "") -> Any:
    if key not in data:
        where = f"{path}." if path else ""
        raise ValueError(f"缺少必填字段 {where}{key!r}")
    return data[key]


def _as_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} 需是对象，收到 {type(value).__name__}")
    return value


def _as_str(value: object, path: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} 需是字符串，收到 {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ValueError(f"{path} 不能为空")
    return value


def _as_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} 需是整数，收到 {type(value).__name__}")
    return value


def _as_seconds(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} 需是非负有限数字，收到 {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{path} 需是非负有限数字，收到 {value!r}")
    return number


def _as_str_map(value: object, path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, item in _as_mapping(value, path).items():
        if not isinstance(key, str):
            raise ValueError(f"{path} 的键需是字符串，收到 {key!r}")
        if not isinstance(item, str):
            raise ValueError(f"{path}.{key} 需是字符串，收到 {type(item).__name__}")
        out[key] = item
    return out


def _as_any_map(value: object, path: str) -> dict[str, Any]:
    out = dict(_as_mapping(value, path))
    for key in out:
        if not isinstance(key, str):
            raise ValueError(f"{path} 的键需是字符串，收到 {key!r}")
    return out


def _as_int_map(value: object, path: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, item in _as_mapping(value, path).items():
        if not isinstance(key, str):
            raise ValueError(f"{path} 的键需是字符串，收到 {key!r}")
        out[key] = _as_int(item, f"{path}.{key}")
    return out


# -- 测试计划 body 解析（§4.1 / §4.4） ---------------------------------------


class _PlanFields(NamedTuple):
    """计划 body 解析结果（save_plan 入参，plan_id 由路径另行给出）。"""

    name: str
    description: str
    labels: dict[str, str] | None
    defaults: dict[str, Any] | None
    steps: list[dict[str, Any]] | None
    created_by: str


def _plan_body(data: Mapping[str, Any]) -> _PlanFields:
    """计划 body → save_plan 入参；形状 / 类型问题统一 InvalidSpec（400）。

    语义校验（步骤 name 唯一、material_id 悬空 / 归属匹配等）在核心 save_plan
    逐条抛 InvalidSpec，本层只做传输形状解析；缺省字段给 None 交核心落空值。
    """
    try:
        name = _as_str(_require(data, "name"), "name", allow_empty=False)
        description = _as_str(data.get("description", ""), "description")
        labels_raw = data.get("labels")
        defaults_raw = data.get("defaults")
        steps_raw = data.get("steps")
        return _PlanFields(
            name=name,
            description=description,
            labels=_as_str_map(labels_raw, "labels") if labels_raw is not None else None,
            defaults=_as_any_map(defaults_raw, "defaults") if defaults_raw is not None else None,
            steps=_plan_steps(steps_raw) if steps_raw is not None else None,
            created_by=_as_str(data.get("created_by", ""), "created_by"),
        )
    except ValueError as exc:
        raise InvalidSpec(str(exc)) from exc


def _plan_steps(value: object) -> list[dict[str, Any]]:
    """steps 数组解析：逐项须是对象（形状校验，语义校验在核心）。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"steps 需是数组，收到 {type(value).__name__}")
    return [dict(_as_mapping(item, f"steps[{index}]")) for index, item in enumerate(value)]


# -- HTTP 辅助 ----------------------------------------------------------------


def _task_section(detail: Mapping[str, Any]) -> dict[str, object]:
    """详情里的任务主体：门面嵌套 ``task`` 段直接用，平铺形态则剥离伴生段。"""
    nested = detail.get("task")
    if isinstance(nested, Mapping):
        return dict(nested)
    return {
        key: value
        for key, value in detail.items()
        if key not in ("stages", "history", "extensions", "extension_errors")
    }


def _row_id(row: Mapping[str, object]) -> int:
    """日志行 id（游标 / 旧→新排序用）；缺失或非整数按 0。"""
    value = row.get("id")
    return value if isinstance(value, int) else 0


def _error_response(status_code: int, message: str, code: str) -> JSONResponse:
    """业务错误响应：``{"ok": false, "error", "code"}`` + 恰当状态码（DESIGN §12）。"""
    return JSONResponse(
        {"ok": False, "error": message, "code": code},
        status_code=status_code,
    )


#: 超限请求的引流额度：拒绝前最多再读这么多字节丢弃，防无限长流挂死
_DRAIN_SLACK: int = 256 * 1024


async def _read_body_capped(request: Request, *, limit: int) -> bytes:
    """读请求体（超限抛 :class:`_BodyTooLarge` → 413）。

    超限不立刻中断读取，而是继续引流丢弃（最多再读 :data:`_DRAIN_SLACK` 字节）
    再拒绝：请求体读完后连接才能优雅收尾（webapi plugin_manager 先例）。
    """
    chunks: list[bytes] = []
    total = 0
    overflow = False
    async for chunk in request.stream():
        total += len(chunk)
        if overflow or total > limit:
            overflow = True
            chunks.clear()
            if total > limit + _DRAIN_SLACK:
                break  # 引流额度用完：不再陪恶意长流耗着
            continue
        chunks.append(chunk)
    if overflow:
        raise _BodyTooLarge(f"请求体超限（上限 {limit} 字节）")
    return b"".join(chunks)


def _required_json_object(raw: bytes) -> Mapping[str, Any]:
    """请求体 → JSON 对象；空体 / 非对象按参数错误抛 ValueError。"""
    if not raw.strip():
        raise ValueError("请求体不能为空")
    return _json_object(raw)


def _optional_json_object(raw: bytes) -> Mapping[str, Any]:
    """请求体 → JSON 对象；空体视作 ``{}``（body 可选的接口用）。"""
    if not raw.strip():
        return {}
    return _json_object(raw)


def _json_object(raw: bytes) -> Mapping[str, Any]:
    data = json.loads(raw)  # 解析失败抛 JSONDecodeError（→ 400 invalid_json）
    if not isinstance(data, Mapping):
        raise ValueError(f"请求体需是 JSON 对象，收到 {type(data).__name__}")
    return data


def _parse_int(raw: str | None, *, default: int, minimum: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} 需是整数，收到 {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} 需 ≥ {minimum}，收到 {value}")
    return value


def _parse_labels(raw: str) -> dict[str, str] | None:
    """labels 过滤：JSON 对象（``{"k":"v"}``）或 ``k=v,k2=v2`` 逗号分隔；空 = 不过滤。"""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        return _as_str_map(json.loads(text), "labels")
    out: dict[str, str] = {}
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        key, sep, value = piece.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"labels 片段需是 k=v 形式，收到 {piece!r}")
        out[key.strip()] = value.strip()
    return out


def _local_uri_path(uri: str) -> Path:
    """``local://…`` URI → 本地路径（兼容 ``local:///D:/x`` 的绝对盘符形态）。"""
    raw = uri[len("local://") :]
    if raw.startswith("/") and len(raw) >= 3 and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


__all__: list[str] = ["TASK_STATES", "TestCoreApiService", "spec_from_dict"]
