"""executor_pytest 管理端 API 服务（``ctx.executor_pytest_master_api``）。

管理 API 独立成服务、``inject = ["executor_pytest_master", "webapi"]`` 注入式
挂载（同 test-core / heartbeat-server 模式）：两者就位 cordis 才装载并挂路由，
缺席安静 PENDING。数据操作一律走 ``ctx.test_core``
（:class:`~common.test_contracts.TestCoreInterface`）与核心服务门面
``ctx.executor_pytest_master``，禁止直连 db。

族内端点（DESIGN §8；材料 CRUD 已由核心 ``/api/test/materials`` 承担，
不重复实现）：

* ``POST /api/pytest/materials/{material_id}/selections`` —— 保存选择集；
* ``POST /api/pytest/materials/{material_id}/push`` —— 预推材料（§5.2）；
* ``POST /api/pytest/materials/{material_id}/inspect`` —— 触发分析 / 解析；
* ``GET /api/pytest/inspects/{request_id}`` —— inspect 状态 / 结果；
* ``GET /api/pytest/stats`` —— 运行统计（§14）。

错误映射沿 test-core 惯例 ``{"ok", "error", "code"}``：MaterialNotFound→404、
MaterialInUse→409、NoCandidateNode→503、JSON / 参数错→400、其余→500。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Any, ClassVar, cast

from cordis_port import Context, Service
from starlette.requests import Request
from starlette.responses import JSONResponse

from common.test_contracts import MaterialInUse, MaterialNotFound, TestCoreInterface

from .material import NoCandidateNode, require_core

if TYPE_CHECKING:  # 运行期无环：plugin 导入本模块装载子服务
    from .plugin import ExecPytestMasterPlugin


# -- 错误边界（统一异常映射，DESIGN §8） -------------------------------------


def _guarded(
    fn: Callable[..., Awaitable[dict[str, object] | JSONResponse]],
) -> Callable[..., Awaitable[dict[str, object] | JSONResponse]]:
    """把 handler 的异常映射为错误响应：``{"ok": false, "error", "code"}``。

    映射表：MaterialNotFound→404、MaterialInUse→409、NoCandidateNode→503、
    JSON 解析（json.JSONDecodeError）→400、参数校验（ValueError）→400、
    其余→500（不泄漏堆栈，只记 ``ctx.logger``）。
    """

    @wraps(fn)
    async def wrapper(
        self: ExecPytestMasterApiService, request: Request, *args: object, **kwargs: object
    ) -> dict[str, object] | JSONResponse:
        try:
            return await fn(self, request, *args, **kwargs)
        except MaterialNotFound as exc:
            return _error_response(404, str(exc) or "材料 / 选择集不存在", "not_found")
        except MaterialInUse as exc:
            return _error_response(409, str(exc) or "材料被引用，禁止删除", "material_in_use")
        except NoCandidateNode as exc:
            return _error_response(503, str(exc) or "无可用执行节点", "no_candidates")
        except json.JSONDecodeError as exc:
            return _error_response(400, f"请求体不是合法 JSON：{exc}", "invalid_json")
        except ValueError as exc:
            return _error_response(400, str(exc) or "参数不合法", "bad_request")
        except Exception as exc:  # noqa: BLE001 —— 兜底 500，禁止泄漏堆栈
            self.ctx.logger.error("pytest 执行器管理接口 %s 意外失败：%s", fn.__name__, exc)
            return _error_response(500, "服务器内部错误", "internal_error")

    return wrapper


def _error_response(status_code: int, message: str, code: str) -> JSONResponse:
    """业务错误响应：``{"ok": false, "error", "code"}`` + 恰当状态码（DESIGN §8）。"""
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
    return cast("Mapping[str, Any]", data)


def _string_list(value: object, name: str) -> list[str]:
    """字符串列表校验（选择集 case_ids / selection_ids 用）。"""
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in cast("list[object]", value)
    ):
        raise ValueError(f"{name} 需是字符串列表")
    return cast("list[str]", value)


class ExecPytestMasterApiService(Service[None]):
    """executor_pytest 管理端 API 服务：族内材料 / inspect / 统计接口（§8）。"""

    #: 附属服务标记：随宿主 ``executor-pytest-master`` 一起呈现，不单列插件
    service_of: ClassVar[str] = "executor-pytest-master"
    #: 服务名（即 ``ctx.executor_pytest_master_api``）
    provide: str | None = "executor_pytest_master_api"
    #: fiber 名（路由归属与诊断用）
    name: str = "executor-pytest-master_api"
    #: 依赖集 = 自家核心 ``executor_pytest_master`` + Web 服务 ``webapi``
    inject: ClassVar[list[str]] = ["executor_pytest_master", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.executor_pytest_master_api
        self.ctx: Context = ctx

    async def init(self) -> object:
        """挂族内管理接口与健康指标（依赖集由 fiber 保证就绪，无需轮询）。

        路由注册绑定调用方 fiber：本服务卸载 / 重载时自动撤销重挂；健康指标
        句柄作为清理函数返回，随 fiber 卸载注销。
        """
        api = self.ctx.webapi
        api.post(
            "/api/pytest/materials/{material_id}/selections",
            self._selection_save,
            kind="api",
            name="pytest-master.selection-save",
        )
        api.post(
            "/api/pytest/materials/{material_id}/push",
            self._material_push,
            kind="api",
            name="pytest-master.material-push",
        )
        api.post(
            "/api/pytest/materials/{material_id}/inspect",
            self._material_inspect,
            kind="api",
            name="pytest-master.material-inspect",
        )
        api.get(
            "/api/pytest/inspects/{request_id}",
            self._inspect_status,
            kind="api",
            name="pytest-master.inspect",
        )
        api.get("/api/pytest/stats", self._stats, kind="api", name="pytest-master.stats")
        return api.add_health_provider("executor_pytest_master", self._health)

    @property
    def _master(self) -> ExecPytestMasterPlugin:
        return cast("ExecPytestMasterPlugin", self.ctx.get("executor_pytest_master"))

    @property
    def _core(self) -> TestCoreInterface:
        return require_core(self.ctx)

    def _health(self) -> Mapping[str, object]:
        """健康指标（stats() 同源，只读内存计数）。"""
        return dict(self._master.stats())

    # -- 族内管理 API（DESIGN §8） -------------------------------------------
    @_guarded
    async def _selection_save(
        self, request: Request, material_id: str
    ) -> dict[str, object] | JSONResponse:
        """保存选择集：``{"name", "case_ids"}`` → ``{"ok", "selection_id"}``。"""
        data = _required_json_object(await request.body())
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name 必填（非空字符串）")
        case_ids = _string_list(data.get("case_ids"), "case_ids")
        selection_id = await self._core.save_selection(material_id, name.strip(), case_ids)
        return {"ok": True, "selection_id": selection_id}

    @_guarded
    async def _material_push(
        self, request: Request, material_id: str
    ) -> dict[str, object] | JSONResponse:
        """预推材料（§5.2）：``{"selection_ids": [...]}`` 可选 → 各节点结果。"""
        data = _optional_json_object(await request.body())
        selection_ids: list[str] | None = None
        if data.get("selection_ids") is not None:
            selection_ids = _string_list(data.get("selection_ids"), "selection_ids")
        result = await self._master.push_material(material_id, selection_ids=selection_ids)
        return {
            "ok": True,
            "material_id": material_id,
            "pushed": list(result.get("pushed", [])),
            "failed": list(result.get("failed", [])),
        }

    @_guarded
    async def _material_inspect(
        self, request: Request, material_id: str
    ) -> dict[str, object] | JSONResponse:
        """触发分析 / 解析：``{"mode": "analyze"|"collect"}`` → ``{"ok", "request_id"}``。

        无可用候选节点映射 503（no_candidates）；mode 不合法 400。
        """
        data = _required_json_object(await request.body())
        mode = data.get("mode")
        if mode not in ("analyze", "collect"):
            raise ValueError(f"mode 需是 analyze / collect，收到 {mode!r}")
        request_id = await self._master.inspect(material_id, mode)
        return {"ok": True, "request_id": request_id}

    @_guarded
    async def _inspect_status(
        self, _request: Request, request_id: str
    ) -> dict[str, object] | JSONResponse:
        """inspect 请求状态 / 结果；未知 request_id 404。"""
        record = await self._master.inspect_status(request_id)
        if record is None:
            return _error_response(404, f"找不到 inspect 请求：{request_id}", "not_found")
        return {"ok": True, "inspect": dict(record)}

    @_guarded
    async def _stats(self, _request: Request) -> dict[str, object] | JSONResponse:
        """运行统计（§14）。"""
        return {"ok": True, "stats": dict(self._master.stats())}


__all__: list[str] = ["ExecPytestMasterApiService"]
