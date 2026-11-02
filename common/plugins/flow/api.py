"""flow 的 API 服务（``ctx.flow_api``，``provide = "flow_api"``）：Phase 3–5 HTTP 面。

「页面 / 界面即服务」约定：API 层是独立服务，``inject = ["flow", "webapi"]``
——两者就位才装载、任一换装 / 重载随依赖纪元自动重挂；headless / 从节点无
webapi 时本服务 PENDING，``ctx.flow`` 核心不受影响。

路由（附录 D；错误信封 ``{error, code, details}`` 与前端 ``request()`` 约定
兼容，§7.2）：

* ``GET  /api/flow/catalog`` —— 步骤目录（stage / labels.k=v / q 筛选，
  ``scope=cluster`` 合并从节点能力）；
* ``GET  /api/flow/flows`` / ``GET|PUT|DELETE /api/flow/flows/{flow_id}``
  —— 流程列表 / 详情（含版本）/ 保存（新版本）/ 删除（用户流程）；
* ``POST /api/flow/flows/{flow_id}/validate`` —— 预检（只给问题清单）；
* ``POST /api/flow/flows/{flow_id}/runs`` —— 启动运行（202 + run_id）；
* ``GET  /api/flow/runs`` / ``GET /api/flow/runs/{run_id}`` —— 列表（时间过滤）
  / 详情（run + 节点时间线）；
* ``POST /api/flow/runs/{run_id}/cancel`` / ``.../resume`` —— 取消 / 处置；
* ``GET  /api/flow/attachments`` / ``PUT /api/flow/attachments/{attachment_id}``
  —— 附加项建议 / 启用停用（``flow:admin``）。

``actor`` 一律取认证身份（``request.state.auth``，不接受 body 自报，附录 D）；
权限点 ``flow:read|run|edit|admin``（§5.14.6）：用户管理插件到位前持令牌者
即 admin，到位后由 ``authorize`` 阶段拦截器按 :data:`ROUTE_PERMISSIONS` 细化。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, cast

from cordis_port import Context, Service
from starlette.requests import Request

from common.flow_contracts import FlowError, FlowRejected, Overrides

from .service import FlowService, problem_to_dict

#: 路由 → 权限点（§5.14.6；authorize 拦截器的判据表）
ROUTE_PERMISSIONS: dict[str, str] = {
    "flow.catalog": "flow:read",
    "flow.list-flows": "flow:read",
    "flow.flow-detail": "flow:read",
    "flow.save-flow": "flow:edit",
    "flow.delete-flow": "flow:edit",
    "flow.validate": "flow:read",
    "flow.start": "flow:run",
    "flow.list-runs": "flow:read",
    "flow.run-detail": "flow:read",
    "flow.cancel": "flow:run",
    "flow.resume": "flow:run",
    "flow.attachments": "flow:read",
    "flow.attachment-toggle": "flow:admin",
}

#: 权限点 → 允许角色（当前模型：持令牌者即 admin / operator，见 §5.14.6）
_PERMISSION_ROLES: dict[str, tuple[str, ...]] = {
    "flow:read": ("admin", "operator"),
    "flow:run": ("admin", "operator"),
    "flow:edit": ("admin",),
    "flow:admin": ("admin",),
}

#: 请求体解析失败的哨兵（与空对象 ``{}`` 区分）
_BAD_BODY: dict[str, Any] = {"__bad_body__": True}


class FlowApiService(Service[None]):
    """flow API 服务：目录 / 流程 CRUD / 运行 / 附加项 HTTP 接口。"""

    #: 附属服务标记：随宿主插件 flow 一起呈现，不单列插件清单
    service_of: ClassVar[str] = "flow"
    #: 服务名（即 ``ctx.flow_api``）
    provide: str | None = "flow_api"
    #: fiber 名（诊断与贡献归属用）
    name: str = "flow_api"
    #: API 层依赖集（flow 核心 + Web 依赖全部圈在此处）
    inject: ClassVar[list[str]] = ["flow", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.flow_api
        self.ctx: Context = ctx

    def init(self) -> None:
        """挂路由 + 健康探针（路由绑定 fiber，卸载自动撤销）。"""
        api = self.ctx.webapi
        api.get("/api/flow/catalog", self._catalog, kind="api", name="flow.catalog")
        api.get("/api/flow/flows", self._list_flows, kind="api", name="flow.list-flows")
        api.get(
            "/api/flow/flows/{flow_id}", self._flow_detail,
            kind="api", name="flow.flow-detail",
        )
        api.put(
            "/api/flow/flows/{flow_id}", self._save_flow,
            kind="api", name="flow.save-flow",
        )
        api.delete(
            "/api/flow/flows/{flow_id}", self._delete_flow,
            kind="api", name="flow.delete-flow",
        )
        api.post(
            "/api/flow/flows/{flow_id}/validate", self._validate,
            kind="api", name="flow.validate",
        )
        api.post(
            "/api/flow/flows/{flow_id}/runs", self._start,
            kind="api", name="flow.start",
        )
        api.get("/api/flow/runs", self._list_runs, kind="api", name="flow.list-runs")
        api.get(
            "/api/flow/runs/{run_id}", self._run_detail,
            kind="api", name="flow.run-detail",
        )
        api.post(
            "/api/flow/runs/{run_id}/cancel", self._cancel,
            kind="api", name="flow.cancel",
        )
        api.post(
            "/api/flow/runs/{run_id}/resume", self._resume,
            kind="api", name="flow.resume",
        )
        api.get(
            "/api/flow/attachments", self._attachments,
            kind="api", name="flow.attachments",
        )
        api.put(
            "/api/flow/attachments/{attachment_id}", self._attachment_toggle,
            kind="api", name="flow.attachment-toggle",
        )
        dispose = api.add_health_provider("flow", self._flow_metrics)
        self.ctx.fiber.effect(lambda: dispose, "flow_api.health_provider")

    # ------------------------------------------------------------ 目录 / 校验
    async def _catalog(self, request: Request) -> dict[str, Any]:
        """``GET /api/flow/catalog``：?stage=...&labels.k=v&q=...&scope=cluster。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.catalog")
        if denied is not None:
            return denied
        labels: dict[str, str] = {}
        for key, value in request.query_params.items():
            if key.startswith("labels."):
                labels[key[len("labels.") :]] = value
        raw_labels = request.query_params.get("labels")
        if raw_labels:
            try:
                parsed: object = json.loads(raw_labels)
            except ValueError:
                return self._error("labels 不是合法 JSON 对象", 400, code="bad_request")
            if isinstance(parsed, dict):
                labels.update({str(k): str(v) for k, v in parsed.items()})
        steps = flow.catalog(
            stage=request.query_params.get("stage"),
            labels=labels or None,
            q=request.query_params.get("q"),
            scope=request.query_params.get("scope"),
        )
        return {"steps": steps}

    async def _validate(self, request: Request, flow_id: str) -> dict[str, Any]:
        """``POST /api/flow/flows/{flow_id}/validate``：预检问题清单（不产生 run）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.validate")
        if denied is not None:
            return denied
        body = await self._body(request)
        if body is _BAD_BODY:
            return self._error("请求体不是合法 JSON 对象", 400, code="bad_request")
        try:
            problems = flow.preflight(flow_id, self._overrides(body))
        except FlowError as exc:
            return self._flow_error(exc)
        return {"problems": [problem_to_dict(problem) for problem in problems]}

    # ------------------------------------------------------------- 流程 CRUD
    async def _list_flows(self, request: Request) -> dict[str, Any]:
        """``GET /api/flow/flows``：模板 + 用户流程（最新版本）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.list-flows")
        if denied is not None:
            return denied
        return {"flows": flow.list_flows()}

    async def _flow_detail(self, request: Request, flow_id: str) -> dict[str, Any]:
        """``GET /api/flow/flows/{flow_id}``：详情（含全部版本 + 导出体）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.flow-detail")
        if denied is not None:
            return denied
        try:
            return await flow.flow_detail(flow_id)
        except FlowError as exc:
            return self._flow_error(exc)

    async def _save_flow(self, request: Request, flow_id: str) -> dict[str, Any]:
        """``PUT /api/flow/flows/{flow_id}``：保存用户流程（生成新版本）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.save-flow")
        if denied is not None:
            return denied
        body = await self._body(request)
        if body is _BAD_BODY:
            return self._error("请求体不是合法 JSON 对象", 400, code="bad_request")
        try:
            return await flow.save_flow(flow_id, body, actor=self._actor(request))
        except FlowRejected as exc:
            return self._rejected(exc)
        except FlowError as exc:
            return self._flow_error(exc)

    async def _delete_flow(self, request: Request, flow_id: str) -> dict[str, Any]:
        """``DELETE /api/flow/flows/{flow_id}``：删除用户流程（模板 → 409）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.delete-flow")
        if denied is not None:
            return denied
        try:
            await flow.delete_flow(flow_id, actor=self._actor(request))
        except FlowError as exc:
            return self._flow_error(exc)
        return {"ok": True}

    # ----------------------------------------------------------------- 运行
    async def _start(self, request: Request, flow_id: str) -> dict[str, Any]:
        """``POST /api/flow/flows/{flow_id}/runs``：启动运行（202 + run_id）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.start")
        if denied is not None:
            return denied
        body = await self._body(request)
        if body is _BAD_BODY:
            return self._error("请求体不是合法 JSON 对象", 400, code="bad_request")
        input_data = body.get("input")
        try:
            handle = await flow.start(
                flow_id,
                input_data if isinstance(input_data, dict) else {},
                overrides=self._overrides(body),
                idempotency_key=(
                    str(body["idempotency_key"]) if body.get("idempotency_key") else None
                ),
                actor=self._actor(request),
            )
        except FlowRejected as exc:
            return self._rejected(exc)
        except FlowError as exc:
            return self._flow_error(exc)
        return self._json({"run_id": handle.id, "state": handle.state.value}, 202)

    async def _list_runs(self, request: Request) -> dict[str, Any]:
        """``GET /api/flow/runs``：?flow_id=&state=&from=&to=&limit=&offset=。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.list-runs")
        if denied is not None:
            return denied
        try:
            limit = max(1, min(int(request.query_params.get("limit", "20")), 200))
            offset = max(0, int(request.query_params.get("offset", "0")))
            from_ts = float(request.query_params["from"]) if request.query_params.get("from") else None
            to_ts = float(request.query_params["to"]) if request.query_params.get("to") else None
        except ValueError:
            return self._error("limit / offset / from / to 不是数字", 400, code="bad_request")
        return await flow.list_runs(
            flow_id=request.query_params.get("flow_id"),
            state=request.query_params.get("state"),
            from_ts=from_ts, to_ts=to_ts,
            limit=limit, offset=offset,
        )

    async def _run_detail(self, request: Request, run_id: str) -> dict[str, Any]:
        """``GET /api/flow/runs/{run_id}``：run + 节点时间线。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.run-detail")
        if denied is not None:
            return denied
        status = await flow.status(run_id)
        if status is None:
            return self._error(f"运行不存在：{run_id}", 404, code="not_found")
        return status

    async def _cancel(self, request: Request, run_id: str) -> dict[str, Any]:
        """``POST /api/flow/runs/{run_id}/cancel``：取消（终态返回 409）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.cancel")
        if denied is not None:
            return denied
        body = await self._body(request)
        if body is _BAD_BODY:
            return self._error("请求体不是合法 JSON 对象", 400, code="bad_request")
        status = await flow.status(run_id)
        if status is None:
            return self._error(f"运行不存在：{run_id}", 404, code="not_found")
        ok = await flow.cancel(
            run_id, str(body.get("reason") or ""), actor=self._actor(request)
        )
        if not ok:
            return self._error("运行已到终态", 409, code="conflict")
        return {"ok": True}

    async def _resume(self, request: Request, run_id: str) -> dict[str, Any]:
        """``POST /api/flow/runs/{run_id}/resume``：人工处置（retry / skip / abort）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.resume")
        if denied is not None:
            return denied
        body = await self._body(request)
        if body is _BAD_BODY:
            return self._error("请求体不是合法 JSON 对象", 400, code="bad_request")
        mode = str(body.get("mode") or "retry")
        status = await flow.status(run_id)
        if status is None:
            return self._error(f"运行不存在：{run_id}", 404, code="not_found")
        if status["state"] != "interrupted":
            return self._error("仅 interrupted 运行可处置", 409, code="conflict")
        try:
            await flow.resume(run_id, mode, actor=self._actor(request))
        except FlowError as exc:
            return self._flow_error(exc)
        return {"ok": True, "mode": mode}

    # -------------------------------------------------------------- 附加项
    async def _attachments(self, request: Request) -> dict[str, Any]:
        """``GET /api/flow/attachments``：附加项建议列表（?flow_id= 标注启用态）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.attachments")
        if denied is not None:
            return denied
        items = flow.attachments()
        wanted = request.query_params.get("flow_id")
        if wanted:
            for item in items:
                item["enabled"] = wanted in cast(list[str], item.get("enabled_in", []))
        return {"attachments": items}

    async def _attachment_toggle(
        self, request: Request, attachment_id: str
    ) -> dict[str, Any]:
        """``PUT /api/flow/attachments/{attachment_id}``：启用 / 停用（写入新版本）。"""
        flow = cast(FlowService, self.ctx.get("flow"))
        denied = self._require(request, "flow.attachment-toggle")
        if denied is not None:
            return denied
        body = await self._body(request)
        if body is _BAD_BODY:
            return self._error("请求体不是合法 JSON 对象", 400, code="bad_request")
        flow_id = str(body.get("flow_id") or "")
        if not flow_id:
            return self._error("缺少 flow_id（附加项启用针对具体用户流程）", 400, code="bad_request")
        try:
            return await flow.set_attachment_enabled(
                attachment_id, flow_id, bool(body.get("enabled", True)),
                actor=self._actor(request),
            )
        except FlowError as exc:
            return self._flow_error(exc)

    # ------------------------------------------------------------- 小工具
    @staticmethod
    def _actor(request: Request) -> str:
        """调用方身份（附录 D：取认证身份，不接受 body 自报）。"""
        identity = getattr(request.state, "auth", None)
        if isinstance(identity, dict):
            return str(identity.get("subject") or "operator")
        return str(identity or "operator")

    def _require(self, request: Request, route_name: str) -> dict[str, Any] | None:
        """权限点校验（§5.14.6）；通过返回 ``None``，拒绝返回 403 信封。

        当前模型：持令牌者即 admin（SharedSecretVerifier）；用户管理插件到位后
        由 ``authorize`` 阶段拦截器按 :data:`ROUTE_PERMISSIONS` 细化。
        """
        point = ROUTE_PERMISSIONS.get(route_name, "flow:read")
        identity = getattr(request.state, "auth", None)
        role = str(identity.get("role")) if isinstance(identity, dict) else ""
        if role not in _PERMISSION_ROLES.get(point, ("admin",)):
            return self._error(
                f"无权限：{point}", 403, code="forbidden", details={"permission": point}
            )
        return None

    async def _body(self, request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            return {}
        try:
            parsed: object = json.loads(raw)
        except ValueError:
            return cast(dict[str, Any], _BAD_BODY)
        if not isinstance(parsed, dict):
            return cast(dict[str, Any], _BAD_BODY)
        return cast(dict[str, Any], parsed)

    @staticmethod
    def _overrides(body: dict[str, Any]) -> Overrides | None:
        raw = body.get("overrides")
        return cast(Overrides, raw) if isinstance(raw, dict) else None

    def _rejected(self, exc: FlowRejected) -> dict[str, Any]:
        return self._error(
            str(exc), 422, code="preflight_failed",
            details={"problems": [problem_to_dict(p) for p in exc.problems]},
        )

    def _flow_error(self, exc: FlowError) -> dict[str, Any]:
        message = str(exc)
        if message.startswith("流程未注册") or message.startswith("运行不存在"):
            return self._error(message, 404, code="not_found")
        if message.startswith("流程不存在"):
            return self._error(message, 404, code="not_found")
        if (
            "不可删除" in message or "不可处置" in message or "状态" in message
            or "重新校验" in message or "已停用" in message or "同名" in message
        ):
            return self._error(message, 409, code="conflict")
        return self._error(message, 400, code="flow_error")

    def _json(self, payload: dict[str, Any], status_code: int) -> dict[str, Any]:
        api = self.ctx.get("webapi")
        return api.json(payload, status_code=status_code)

    def _error(
        self, message: str, status_code: int, *,
        code: str = "error", details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._json(
            {"error": message, "code": code, "details": details or {}}, status_code
        )

    def _flow_metrics(self) -> dict[str, object]:
        flow = cast(FlowService, self.ctx.get("flow"))
        return cast(dict[str, object], flow.metrics())


__all__: list[str] = ["ROUTE_PERMISSIONS", "FlowApiService"]
