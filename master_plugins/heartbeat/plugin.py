"""主模块心跳插件：接收从模块心跳、维护在线状态与能力存档（经 comm 通道）。

职责（设计稿 ``master_plugins/heartbeat/DESIGN.md``）：

1. ``ctx.comm.on("heartbeat.beat")`` 接收从模块心跳（载荷见设计稿 §2），
   并经 comm 下行 ``heartbeat.config``（下发 ``interval`` / ``timeout`` /
   ``max_backoff``）与 ``heartbeat.caps``（请求全量能力）——旧私有
   ``POST /api/heartbeat`` 应答捎带语义由两条下行消息替代；
2. 定时扫描推进在线状态（``online`` / ``offline`` / ``lost``），状态转换
   记入历史并广播 :data:`~master_plugins.heartbeat.registry.NODE_STATUS_EVENT`；
3. 管理 API（节点列表 / 详情 / 移除 / 标记离线 / 跨节点能力查询 / 配置查看）
   拆给 :mod:`.api_service` 的 ``HeartbeatApiService``、从模块管理页
   （``ui/NodesPage.vue``）拆给 :mod:`.ui_service` 的 ``HeartbeatUiService``
   ——核心不沾 webapi / webui，它们缺席 / 晚到都不阻塞心跳与 ``ctx.nodes``。

配置见 :class:`~master_plugins.heartbeat.config.HeartbeatServerConfig`
（``config.ini`` 的 ``[heartbeat]`` 节）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from typing import ClassVar, cast

from cordis_port import Context, Fiber, Service

from .api_service import NODE_EVENT as NODE_EVENT
from .api_service import NODES_EVENT as NODES_EVENT
from .api_service import HeartbeatApiService
from .config import HeartbeatServerConfig
from .registry import NODE_STATUS_EVENT, HeartbeatServerError, NodeRegistry, NodesService, NodeStatus
from .ui_service import HeartbeatUiService

#: fire-and-forget 投递策略（comm DeliveryPolicy 字面量，不 import 对方插件包）：
#: 心跳下行容忍丢失，丢一条下一拍补上
_FIRE_AND_FORGET: dict[str, int] = {"min_success": 0, "max_attempts": 1}


class HeartbeatServerPlugin(Service[HeartbeatServerConfig]):
    """主模块心跳插件（``heartbeat-server``，``provide = "heartbeat_server"``）。

    ``inject`` 注入的是**服务**（不是插件名）：``appconfig`` / ``comm`` 由
    同名插件提供，``node_kind`` / ``node_name`` 是装配时 ``ctx.provide(...)``
    注册的值服务，**没有对应插件**。核心**不依赖 webapi / webui**：管理 API
    拆给 :class:`HeartbeatApiService`（``inject = ["heartbeat_server", "webapi"]``）、
    「从模块管理」页拆给 :class:`HeartbeatUiService`（``inject = ["nodes", "webui"]``，
    同包）——webapi / webui 未就绪附属服务安静 PENDING，晚出现自动挂载。
    """

    name: ClassVar[str] = "heartbeat-server"
    #: 服务名（附属服务按 ``heartbeat_server`` 声明核心依赖，经 ``ctx.get`` 取用）
    provide: str | None = "heartbeat_server"
    #: 核心依赖集无 webapi / webui：Web 侧拆给附属服务（缺席 / 晚到不阻塞核心）
    inject: ClassVar[list[str]] = ["appconfig", "comm", "node_kind", "node_name"]
    #: 配置模型（标准 Schema）：驱动校验、默认值注入与写回
    Config: ClassVar[type[HeartbeatServerConfig]] = HeartbeatServerConfig

    def __init__(
        self, ctx: Context, config: HeartbeatServerConfig | None = None
    ) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.heartbeat_server
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
        #: 管理 API / 页面服务 fiber：随本插件 fiber 自动清理
        self._api_fiber: Fiber | None = None
        self._ui_fiber: Fiber | None = None

    # -- 生命周期 -------------------------------------------------------------
    async def init(self) -> object:
        """注册 comm 收件 / 节点目录服务并启动状态扫描；返回卸载清理函数。"""
        # 节点目录服务（GAP-3）：引擎经 ctx.nodes 选节点，契约在 common/；
        # provide 绑本 fiber 生命周期，卸载自动注销
        self.ctx.provide("nodes", NodesService(self._registry))
        self.ctx.comm.on("heartbeat.beat", self._on_beat)
        # 管理 API / 管理页拆附属服务分批装载（inject 依赖集就绪即装载）：
        # fiber 挂在本插件生命周期下，卸载 / 热更自动带走。不能 await——
        # webapi / webui 缺席时 fiber 会停在 PENDING（无头部署）。
        self._api_fiber = self.ctx.plugin(HeartbeatApiService)
        self._ui_fiber = self.ctx.plugin(HeartbeatUiService)
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

    def health_metrics(self) -> dict[str, object]:
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
            self.push_config(),
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

    def push_config(self) -> dict[str, float]:
        """下发给从模块的运行参数（``heartbeat.config`` 载荷）。"""
        return {
            "interval": self.config.interval,
            "timeout": self.config.timeout,
            "max_backoff": self.config.max_backoff,
        }

    # -- 管理查询门面（HTTP 层见 api_service.HeartbeatApiService）--------------
    def node_records(self) -> list[dict[str, object]]:
        """全部节点记录的可序列化快照（按 node_id 稳定排序）。"""
        return [record.to_dict() for record in self._registry.nodes()]

    def node_detail(self, node_id: str) -> dict[str, object] | None:
        """节点详情（能力信封 / 未解析项 / 历史）；找不到返回 None。"""
        record = self._registry.get(node_id)
        if record is None:
            return None
        return {
            "node": record.to_dict(),
            "capabilities": [cap.to_dict() for cap in record.caps],
            "unparsed": list(record.unparsed),
            "history": self._registry.history(node_id),
        }

    def remove_node(self, node_id: str) -> bool:
        """移除节点记录（含历史）；找不到返回 False。"""
        return self._registry.remove(node_id)

    def capability_entries(
        self,
        *,
        kinds: Collection[str] | None = None,
        labels: Mapping[str, str] | None = None,
        q: str | None = None,
    ) -> list[dict[str, object]]:
        """跨节点能力查询：``kinds``（多选）/ ``labels``（子集）/ ``q``（关键字）。"""
        return self._registry.capabilities(kinds=kinds, labels=labels, q=q)

    def config_view(self) -> dict[str, object]:
        """当前生效的配置（只读）：判定阈值、下发参数与本主模块身份。"""
        return {
            "config": self.config.to_dict(),
            "push": self.push_config(),
            "master": {
                "node_id": str(self.ctx.node_name),
                "kind": str(self.ctx.node_kind),
            },
        }

__all__: list[str] = [
    "NODES_EVENT",
    "NODE_EVENT",
    "HeartbeatServerPlugin",
    "NodeStatus",
]
