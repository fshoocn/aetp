"""主模块心跳插件：接收从模块心跳、维护在线状态与能力存档（经 comm 通道）。

职责（设计稿 ``master_plugins/heartbeat/DESIGN.md``）：

1. ``ctx.comm.on("heartbeat.beat")`` 接收从模块心跳（载荷见设计稿 §2），
   并经 comm 下行 ``heartbeat.config``（下发 ``interval`` / ``timeout`` /
   ``max_backoff``）与 ``heartbeat.caps``（请求全量能力）——旧私有
   ``POST /api/heartbeat`` 应答捎带语义由两条下行消息替代；
2. 定时扫描推进在线状态（``online`` / ``offline`` / ``lost``），状态转换
   记入历史并广播 :data:`~master_plugins.heartbeat.registry.NODE_STATUS_EVENT`；
3. 管理 API（节点列表 / 详情 / 移除 / 标记离线 / 跨节点能力查询 / 配置查看）
   与从模块管理页（``ui/NodesPage.vue``）。

配置见 :class:`~master_plugins.heartbeat.config.HeartbeatServerConfig`
（``config.ini`` 的 ``[heartbeat]`` 节）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar, cast

from cordis_port import Context, EventDispatchError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import HeartbeatServerConfig
from .registry import NODE_STATUS_EVENT, HeartbeatServerError, NodeRegistry, NodeStatus

#: fire-and-forget 投递策略（comm DeliveryPolicy 字面量，不 import 对方插件包）：
#: 心跳下行容忍丢失，丢一条下一拍补上
_FIRE_AND_FORGET: dict[str, int] = {"min_success": 0, "max_attempts": 1}

#: 节点列表接口的扩展收集事件（与从模块侧 status 接口同一契约：
#: 监听器把自家数据写进 ``response["extensions"]["<插件名>"]``）
NODES_EVENT = "heartbeat/collect-nodes"
#: 节点详情接口的扩展收集事件（同上）
NODE_EVENT = "heartbeat/collect-node"


class HeartbeatServerPlugin:
    """主模块心跳插件（``heartbeat-server``）。

    ``inject`` 注入的是**服务**（不是插件名）：``appconfig`` / ``webapi`` /
    ``webui`` 由同名插件提供，``node_kind`` / ``node_name`` 是装配时
    ``ctx.provide(...)`` 注册的值服务，**没有对应插件**。
    """

    name: ClassVar[str] = "heartbeat-server"
    inject: ClassVar[list[str]] = ["appconfig", "webapi", "webui", "comm", "node_kind", "node_name"]
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
        """注册 API 路由 / comm 收件 / 管理页并启动状态扫描；返回卸载清理函数。"""
        api = self.ctx.webapi
        self.ctx.comm.on("heartbeat.beat", self._on_beat)
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
        dispose = api.add_health_provider("heartbeat", self._health_metrics)
        self.ctx.fiber.effect(lambda: dispose, "heartbeat-server.health")
        self.ctx.webui.register_ui(
            "nodes",
            kind="page",
            format="vue",
            resource_root=str(Path(__file__).resolve().parent / "ui"),
            entry="NodesPage.vue",
            path="/nodes",
            title="从模块管理",
            menu_group="",
            menu_icon="pi pi-desktop",
            menu_order=99,
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

    def _health_metrics(self) -> dict[str, object]:
        """/healthz 指标：节点数与在线状态统计（只读内存计数）。"""
        nodes = self._registry.nodes()
        return {
            "nodes_total": len(nodes),
            "online": sum(1 for record in nodes if record.status is NodeStatus.ONLINE),
            "offline": sum(1 for record in nodes if record.status is NodeStatus.OFFLINE),
            "lost": sum(1 for record in nodes if record.status is NodeStatus.LOST),
        }

    def _broadcast(self, event: str, payload: dict[str, object]) -> None:
        if event == NODE_STATUS_EVENT:
            # 状态机转换统一补日志（对称 slave 侧）：旧态 → 新态 + 节点
            self.ctx.logger.info(
                "节点 %s 状态转换：%s → %s",
                payload.get("node_id"),
                payload.get("from"),
                payload.get("to"),
            )
        self.ctx.events.emit(event, payload)

    def _warn(self, message: str) -> None:
        self.ctx.logger.warning("%s", message)

    # -- 心跳接收（comm 收件，协议 §2） ---------------------------------------
    def _on_beat(self, payload: object, _meta: object) -> None:
        """``heartbeat.beat``（``ctx.comm.on``）：upsert 上报并下行下发参数。

        旧私有应答的两个字段改为 comm 下行消息（FIRE_AND_FORGET，不落盘）：
        ``heartbeat.config`` 每拍推送运行参数（自愈两侧重启，保持
        「最迟一个心跳周期同步」语义）；``heartbeat.caps`` 按需请求全量能力。
        """
        if not isinstance(payload, dict):
            self._warn("心跳载荷不是对象，忽略")
            return
        try:
            record, need_caps = self._registry.accept(
                cast(dict[str, object], payload)
            )
        except HeartbeatServerError as error:
            self._warn(f"心跳载荷被拒（{error.code}）：{error}")
            return
        node_id = record.node_id
        self.ctx.comm.send_to(
            node_id,
            "heartbeat.config",
            self._push_config(),
            policy=_FIRE_AND_FORGET,
            immediate=True,
        )
        if need_caps:
            self.ctx.comm.send_to(
                node_id,
                "heartbeat.caps",
                {"need_caps": True},
                policy=_FIRE_AND_FORGET,
                immediate=True,
            )

    def _push_config(self) -> dict[str, float]:
        """下发给从模块的运行参数（``heartbeat.config`` 载荷）。"""
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
                "node_id": str(self.ctx.node_name),
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
def _error_response(error: HeartbeatServerError) -> JSONResponse:
    """业务错误响应：``{"error": {"message", "code"}}`` + 恰当状态码（设计稿 §4）。"""
    return JSONResponse(
        {"error": {"message": str(error), "code": error.code}},
        status_code=error.status_code,
    )


__all__: list[str] = [
    "NODES_EVENT",
    "NODE_EVENT",
    "HeartbeatServerPlugin",
    "NodeStatus",
]
