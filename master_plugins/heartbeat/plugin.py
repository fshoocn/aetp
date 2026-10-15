"""主模块心跳插件：接收从模块心跳、维护在线状态与能力存档。

职责（设计稿 ``master_plugins/heartbeat/DESIGN.md``）：

1. ``POST /api/heartbeat`` 接收从模块心跳（协议见设计稿 §2），应答携带
   ``config``（下发 ``interval`` / ``timeout`` / ``max_backoff``）与
   ``need_caps``（请求全量能力）；
2. 定时扫描推进在线状态（``online`` / ``offline`` / ``lost``），状态转换
   记入历史并广播 :data:`~master_plugins.heartbeat.registry.NODE_STATUS_EVENT`；
3. 管理 API（节点列表 / 详情 / 移除 / 标记离线 / 跨节点能力查询 / 配置查看）
   与从模块管理页（``ui/NodesPage.vue``）。

配置见 :class:`~master_plugins.heartbeat.config.HeartbeatServerConfig`
（``config.ini`` 的 ``[heartbeat]`` 节）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context, EventDispatchError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import HeartbeatServerConfig
from .registry import HeartbeatServerError, NodeRegistry, NodeStatus

#: 心跳请求体上限（能力全量也不该超过它；超限 413）
MAX_BODY_BYTES: int = 1024 * 1024

#: 节点列表接口的扩展收集事件（与从模块侧 status 接口同一契约：
#: 监听器把自家数据写进 ``response["extensions"]["<插件名>"]``）
NODES_EVENT = "heartbeat/collect-nodes"
#: 节点详情接口的扩展收集事件（同上）
NODE_EVENT = "heartbeat/collect-node"


class HeartbeatServerPlugin:
    """主模块心跳插件（``heartbeat-server``）。

    ``inject`` 注入的是**服务**（不是插件名）：``appconfig`` / ``webapi`` /
    ``webui`` 由同名插件提供，``node_kind`` 是装配时
    ``ctx.provide("node_kind", kind)`` 注册的值服务，**没有对应插件**。
    """

    name: ClassVar[str] = "heartbeat-server"
    inject: ClassVar[list[str]] = ["appconfig", "webapi", "webui", "node_kind"]
    #: 配置模型（标准 Schema）：驱动校验、默认值注入与写回
    Config: ClassVar[type[HeartbeatServerConfig]] = HeartbeatServerConfig

    def __init__(
        self, ctx: Context, config: HeartbeatServerConfig | None = None
    ) -> None:
        self.ctx: Context = ctx
        self.config: HeartbeatServerConfig = (
            config
            if isinstance(config, HeartbeatServerConfig)
            else HeartbeatServerConfig()
        )
        self._registry: NodeRegistry = NodeRegistry(
            self.config, emit=self._broadcast, log=self._warn
        )
        self._task: asyncio.Task[None] | None = None

    # -- 生命周期 -------------------------------------------------------------
    async def init(self) -> object:
        """注册 API 路由 / 管理页并启动状态扫描；返回值即卸载清理函数。"""
        api = self.ctx.webapi
        api.post("/api/heartbeat", self._receive, kind="api", name="heartbeat-server.receive")
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
        self.ctx.webui.register_ui(
            "nodes",
            kind="page",
            format="vue",
            resource_root=str(Path(__file__).resolve().parent / "ui"),
            entry="NodesPage.vue",
            path="/nodes",
            title="从模块管理",
            menu_group="节点",
            menu_icon="pi pi-desktop",
            menu_order=20,
        )
        self._task = asyncio.create_task(self._sweep_loop())
        self._task.add_done_callback(self._log_task_exit)
        self.ctx.logger.info(
            "主模块心跳已启动：判离线 %s 秒 / 判失联 %s 秒，扫描周期 %s 秒",
            self.config.offline_timeout,
            self.config.lost_timeout,
            self.config.sweep_interval,
        )
        return self._stop

    async def _stop(self) -> None:
        """卸载清理：取消状态扫描并等待其退出（gather 吸收任务自身的取消）。"""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.sweep_interval)
            self._registry.sweep()

    def _log_task_exit(self, task: asyncio.Task[None]) -> None:
        """扫描任务退出的观察哨：意外退出记 error 日志，不再无声消失。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ctx.logger.error("状态扫描任务意外退出：%s", error)

    def _broadcast(self, event: str, payload: dict[str, object]) -> None:
        self.ctx.events.emit(event, payload)

    def _warn(self, message: str) -> None:
        self.ctx.logger.warning("%s", message)

    # -- 心跳接收（协议 §2） ---------------------------------------------------
    async def _receive(self, request: Request) -> dict[str, object] | JSONResponse:
        """``POST /api/heartbeat``：upsert 上报并应答下发参数。"""
        try:
            body = await read_json_body(request)
            record, need_caps = self._registry.accept(body)
        except HeartbeatServerError as error:
            return _error_response(error)
        return {
            "ok": True,
            "server_time": record.last_seen,
            "config": self._push_config(),
            "need_caps": need_caps,
        }

    def _push_config(self) -> dict[str, float]:
        """下发给从模块的运行参数（协议 §2：应答只承载这三个键）。"""
        return {
            "interval": self.config.interval,
            "timeout": self.config.timeout,
            "max_backoff": self.config.max_backoff,
        }

    # -- 管理 API（设计稿 §4） -------------------------------------------------
    async def _nodes(self, _request: Request) -> dict[str, object] | JSONResponse:
        response: dict[str, object] = {
            "nodes": [record.to_dict() for record in self._registry.nodes()]
        }
        await self._extend(response, NODES_EVENT)
        return response

    async def _node_detail(
        self, _request: Request, node_id: str
    ) -> dict[str, object] | JSONResponse:
        record = self._registry.get(node_id)
        if record is None:
            return _error_response(
                HeartbeatServerError(f"找不到节点：{node_id}", 404, code="not_found")
            )
        response: dict[str, object] = {
            "node": record.to_dict(),
            "capabilities": [cap.to_dict() for cap in record.caps],
            "unparsed": list(record.unparsed),
            "history": self._registry.history(node_id),
        }
        await self._extend(response, NODE_EVENT)
        return response

    async def _node_delete(
        self, _request: Request, node_id: str
    ) -> dict[str, object] | JSONResponse:
        if not self._registry.remove(node_id):
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
        entries = self._registry.capabilities(
            kinds=kinds or None,
            labels=labels or None,
            q=params.get("q") or None,
        )
        return {"capabilities": entries, "count": len(entries)}

    async def _config_view(self, _request: Request) -> dict[str, object]:
        """当前生效的配置（只读）：判定阈值、下发参数与本主模块身份。"""
        return {
            "config": self.config.to_dict(),
            "push": self._push_config(),
            "master": {
                "node_id": self.ctx.appconfig.section("node").get("name")
                or str(self.ctx.node_kind),
                "kind": str(self.ctx.node_kind),
            },
        }

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
async def read_json_body(request: Request) -> dict[str, object]:
    """流式读请求体（超限 413），解析成 JSON 对象。"""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HeartbeatServerError(
            f"请求体过大（最大 {MAX_BODY_BYTES} 字节）", 413, code="payload_too_large"
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise HeartbeatServerError(
                f"请求体过大（最大 {MAX_BODY_BYTES} 字节）", 413, code="payload_too_large"
            )
        chunks.append(chunk)
    try:
        data: object = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise HeartbeatServerError(f"请求体不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise HeartbeatServerError("请求体必须是 JSON 对象")
    return cast(dict[str, object], data)


def _error_response(error: HeartbeatServerError) -> JSONResponse:
    """业务错误响应：``{"error": {"message", "code"}}`` + 恰当状态码（设计稿 §4）。"""
    return JSONResponse(
        {"error": {"message": str(error), "code": error.code}},
        status_code=error.status_code,
    )


__all__: list[str] = [
    "MAX_BODY_BYTES",
    "NODES_EVENT",
    "NODE_EVENT",
    "HeartbeatServerPlugin",
    "NodeStatus",
    "read_json_body",
]
