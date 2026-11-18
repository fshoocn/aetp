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
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from cordis_port import Context, EventDispatchError, Service
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from common.test_contracts import (
    EVENT_COLLECT_TASK,
    DuplicateSubmission,
    ExecutorSelector,
    InvalidSpec,
    InvalidTransition,
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
def _guarded(
    fn: Callable[..., dict[str, object] | JSONResponse],
) -> Callable[..., dict[str, object] | JSONResponse]:
    """把 handler 的异常映射为错误响应：``{"ok": false, "error", "code"}``。

    映射表：TaskNotFound→404、InvalidSpec→400、InvalidTransition→409、
    DuplicateSubmission→409、JSON 解析 / 参数校验（ValueError）→400、
    其余→500（不泄漏堆栈，只记 ``ctx.logger``）。
    """

    @wraps(fn)
    async def wrapper(
        self: TestCoreApiService, request: Request, *args: object, **kwargs: object
    ) -> dict[str, object] | JSONResponse:
        try:
            return await fn(self, request, *args, **kwargs)
        except TaskNotFound as exc:
            return _error_response(404, str(exc) or "任务不存在", "not_found")
        except InvalidSpec as exc:
            return _error_response(400, str(exc) or "任务规格无效", "invalid_spec")
        except InvalidTransition as exc:
            return _error_response(409, str(exc) or "非法状态迁移", "invalid_transition")
        except DuplicateSubmission as exc:
            return _error_response(409, str(exc) or "重复提交", "duplicate_submission")
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


def _error_response(status_code: int, message: str, code: str) -> JSONResponse:
    """业务错误响应：``{"ok": false, "error", "code"}`` + 恰当状态码（DESIGN §12）。"""
    return JSONResponse(
        {"ok": False, "error": message, "code": code},
        status_code=status_code,
    )


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
