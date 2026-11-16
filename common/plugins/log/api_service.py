"""log 的 API 服务（``ctx.log_api``，``provide = "log_api"``）：查询接口。

日志核心（LogPlugin，``ctx.log``）必须足够早装载以捕获装配期日志，不沾 Web
依赖；查询 API 独立成本服务、``inject = ["log", "webapi"]`` **注入式挂载**——
依赖自家核心与 Web 服务：两者就位 cordis 才装载并挂路由（依赖缺席安静
PENDING，核心 / webapi 换装、重载随依赖纪元自动重挂），不需要就绪守护轮询。

挂载 ``GET /api/logs``（游标查询）、``GET /api/logs/nodes``（节点清单）、
``DELETE /api/logs``（按筛选删除）与 ``log`` 健康指标；HTTP 参数解析留在本层，
数据操作走核心的公共查询门面（``query_logs`` / ``known_nodes`` /
``delete_logs``）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from cordis_port import Context, Service
from starlette.requests import Request

if TYPE_CHECKING:  # 运行期无环：plugin 导入本模块装载子服务
    from .plugin import LogPlugin


def _as_int(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() else None


def _as_float(raw: str | None) -> float | None:
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class LogApiService(Service[None]):
    """log API 服务：挂查询 / 节点清单 / 删除接口与健康指标。"""

    #: 附属服务标记：本类是 ``log`` 插件的界面层载体之一，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 log 一起呈现
    service_of: ClassVar[str] = "log"
    #: 服务名（即 ``ctx.log_api``）
    provide: str | None = "log_api"
    #: fiber 名（路由归属与诊断用）
    name: str = "log_api"
    #: API 层的依赖集 = 自家核心 ``log`` + Web 服务 ``webapi``：两者就位才装载，
    #: 依赖缺席安静 PENDING（核心因此不沾 webapi 依赖）
    inject: ClassVar[list[str]] = ["log", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.log_api
        self.ctx: Context = ctx

    async def init(self) -> object:
        """挂查询 API 与健康指标（依赖集由 fiber 保证就绪，无需轮询）。

        路由注册绑定调用方 fiber：本服务卸载 / 重载时自动撤销重挂；健康指标
        句柄作为清理函数返回，随 fiber 卸载注销。
        """
        api = self.ctx.webapi
        api.get("/api/logs", self._query, kind="api", name="log.query")
        api.get("/api/logs/nodes", self._nodes, kind="api", name="log.nodes")
        api.delete("/api/logs", self._delete, kind="api", name="log.delete")
        return api.add_health_provider("log", self._log.metrics)

    @property
    def _log(self) -> LogPlugin:
        return cast("LogPlugin", self.ctx.get("log"))

    # ----------------------------------------------------------- 查询接口
    async def _query(self, request: Request) -> dict[str, object]:
        """``GET /api/logs``：游标查询（新 → 旧；``after_id`` 为尾随方向）。"""
        params = request.query_params
        logs = await self._log.query_logs(
            node=params.get("node") or None,
            level=_as_int(params.get("level")),
            q=params.get("q") or None,
            after_id=_as_int(params.get("after_id")),
            before_id=_as_int(params.get("before_id")),
            around_id=_as_int(params.get("around_id")),
            from_ts=_as_float(params.get("from_ts")),
            to_ts=_as_float(params.get("to_ts")),
            limit=int(params.get("limit") or 200),
        )
        return {"logs": logs, "count": len(logs)}

    async def _nodes(self, _request: Request) -> dict[str, object]:
        """``GET /api/logs/nodes``：全部已知节点名（过滤下拉）。"""
        try:
            return {"nodes": await self._log.known_nodes()}
        except Exception as exc:  # noqa: BLE001 —— 查询故障降级为错误响应
            return self._error(f"查询库不可用：{type(exc).__name__}", 503)

    async def _delete(self, request: Request) -> dict[str, object]:
        """``DELETE /api/logs``：按当前筛选删除（审计留痕，防灭迹）。"""
        params = request.query_params
        try:
            deleted = await self._log.delete_logs(
                node=params.get("node") or None,
                level=_as_int(params.get("level")),
                q=params.get("q") or None,
                from_ts=_as_float(params.get("from_ts")),
                to_ts=_as_float(params.get("to_ts")),
            )
        except Exception as exc:  # noqa: BLE001 —— 查询故障降级为错误响应
            return self._error(f"删除失败：{type(exc).__name__}", 503)
        return {"deleted": deleted}

    def _error(self, message: str, status_code: int) -> dict[str, object]:
        return self.ctx.webapi.json({"error": message}, status_code=status_code)


__all__: list[str] = ["LogApiService"]
