"""heartbeat-server 的管理 API 服务（``ctx.heartbeat_server_api``）。

心跳核心（HeartbeatServerPlugin）承载心跳接收 / 在线状态机 / 节点目录，
不沾 Web 依赖；管理 API 独立成本服务、``inject = ["heartbeat_server",
"webapi"]`` **注入式挂载**——两者就位 cordis 才装载并挂路由（依赖缺席安静
PENDING，核心 / webapi 换装、重载随依赖纪元自动重挂）。HTTP 参数解析与
可扩展返回值（``extensions`` 收集槽）留在本层，数据操作走核心公共门面
（``node_records`` / ``node_detail`` 等）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from cordis_port import Context, EventDispatchError, Service
from starlette.requests import Request
from starlette.responses import JSONResponse

from .registry import HeartbeatServerError

if TYPE_CHECKING:  # 运行期无环：plugin 导入本模块装载子服务
    from .plugin import HeartbeatServerPlugin

#: 节点列表接口的扩展收集事件（与从模块侧 status 接口同一契约：
#: 监听器把自家数据写进 ``response["extensions"]["<插件名>"]``）
NODES_EVENT = "heartbeat/collect-nodes"
#: 节点详情接口的扩展收集事件（同上）
NODE_EVENT = "heartbeat/collect-node"


class HeartbeatApiService(Service[None]):
    """heartbeat-server 管理 API 服务：挂节点管理 / 能力 / 配置接口。"""

    #: 附属服务标记：本类是 ``heartbeat-server`` 插件的界面层载体之一，不是独立
    #: 插件——插件清单（builtin_specs）据此不单列，随宿主一起呈现
    service_of: ClassVar[str] = "heartbeat-server"
    #: 服务名（即 ``ctx.heartbeat_server_api``）
    provide: str | None = "heartbeat_server_api"
    #: fiber 名（路由归属与诊断用）
    name: str = "heartbeat-server_api"
    #: 管理 API 的依赖集 = 自家核心 ``heartbeat_server`` + Web 服务 ``webapi``：
    #: 两者就位才装载，依赖缺席安静 PENDING（核心因此不沾 webapi 依赖）
    inject: ClassVar[list[str]] = ["heartbeat_server", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.heartbeat_server_api
        self.ctx: Context = ctx

    async def init(self) -> object:
        """挂管理接口与健康指标（依赖集由 fiber 保证就绪，无需轮询）。

        路由注册绑定调用方 fiber：本服务卸载 / 重载时自动撤销重挂；健康指标
        句柄作为清理函数返回，随 fiber 卸载注销。
        """
        api = self.ctx.webapi
        api.get("/api/heartbeat/nodes", self._nodes, kind="api", name="heartbeat-server.nodes")
        api.get(
            "/api/heartbeat/nodes/{node_id}",
            self._node_detail,
            kind="api",
            name="heartbeat-server.node",
        )
        api.delete(
            "/api/heartbeat/nodes/{node_id}",
            self._node_delete,
            kind="api",
            name="heartbeat-server.node-delete",
        )
        api.get(
            "/api/heartbeat/capabilities",
            self._capabilities,
            kind="api",
            name="heartbeat-server.capabilities",
        )
        api.get(
            "/api/heartbeat/config",
            self._config_view,
            kind="api",
            name="heartbeat-server.config",
        )
        return api.add_health_provider("heartbeat", self._core.health_metrics)

    @property
    def _core(self) -> HeartbeatServerPlugin:
        return cast("HeartbeatServerPlugin", self.ctx.get("heartbeat_server"))

    # -- 管理 API（设计稿 §4） -------------------------------------------------
    async def _nodes(self, _request: Request) -> dict[str, object] | JSONResponse:
        response: dict[str, object] = {"nodes": self._core.node_records()}
        await self._extend(response, NODES_EVENT)
        return response

    async def _node_detail(
        self, _request: Request, node_id: str
    ) -> dict[str, object] | JSONResponse:
        detail = self._core.node_detail(node_id)
        if detail is None:
            return _error_response(
                HeartbeatServerError(f"找不到节点：{node_id}", 404, code="not_found")
            )
        response: dict[str, object] = dict(detail)
        await self._extend(response, NODE_EVENT)
        return response

    async def _node_delete(
        self, _request: Request, node_id: str
    ) -> dict[str, object] | JSONResponse:
        if not self._core.remove_node(node_id):
            return _error_response(
                HeartbeatServerError(f"找不到节点：{node_id}", 404, code="not_found")
            )
        return {"ok": True, "node_id": node_id}

    async def _capabilities(self, request: Request) -> dict[str, object]:
        """跨节点能力查询：``kind``（逗号多选）/ ``labels.k=v``（子集）/ ``q``。"""
        params = request.query_params
        kinds = [part.strip() for part in params.get("kind", "").split(",") if part.strip()]
        labels = {
            key[len("labels.") :]: value
            for key, value in params.items()
            if key.startswith("labels.") and value
        }
        entries = self._core.capability_entries(
            kinds=kinds or None,
            labels=labels or None,
            q=params.get("q") or None,
        )
        return {"capabilities": entries, "count": len(entries)}

    async def _config_view(self, _request: Request) -> dict[str, object]:
        """当前生效的配置（只读）：判定阈值、下发参数与本主模块身份。"""
        return self._core.config_view()

    async def _extend(self, response: dict[str, object], event: str) -> None:
        """可扩展返回值契约（与从模块侧 status 接口同款，设计稿 §4）。

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


# -- HTTP 辅助 ----------------------------------------------------------------
def _error_response(error: HeartbeatServerError) -> JSONResponse:
    """业务错误响应：``{"error": {"message", "code"}}`` + 恰当状态码（设计稿 §4）。"""
    return JSONResponse(
        {"error": {"message": str(error), "code": error.code}},
        status_code=error.status_code,
    )


__all__: list[str] = ["NODES_EVENT", "NODE_EVENT", "HeartbeatApiService"]
