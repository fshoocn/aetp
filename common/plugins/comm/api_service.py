"""comm 的通道 API 服务（``ctx.comm_api``）：``/api/comm/*`` 三条通道接口。

通信核心（CommService，``ctx.comm``）承载消息泵 / outbox / QoS，不沾 Web
依赖；通道 HTTP 接口独立成本服务、``inject = ["comm", "webapi"]``
**注入式挂载**——依赖自家核心与 Web 服务：两者就位 cordis 才装载并挂路由
（依赖缺席安静 PENDING，核心 / webapi 换装、重载随依赖纪元自动重挂），
无头部署只跑消息泵也不受影响。处理器实现留在核心（与内部状态深度耦合），
本服务只做挂载与健康指标注册。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from cordis_port import Context, Service

if TYPE_CHECKING:  # 运行期无环：service 导入本模块装载子服务
    from .service import CommService


class CommApiService(Service[None]):
    """comm 通道 API 服务：挂 ``/api/comm/ingest|poll|ack`` 与健康指标。"""

    #: 附属服务标记：本类是 ``comm`` 插件的界面层载体之一，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 comm 一起呈现
    service_of: ClassVar[str] = "comm"
    #: 服务名（即 ``ctx.comm_api``）
    provide: str | None = "comm_api"
    #: fiber 名（路由归属与诊断用）
    name: str = "comm_api"
    #: 通道 API 的依赖集 = 自家核心 ``comm`` + Web 服务 ``webapi``：两者就位
    #: 才装载，依赖缺席安静 PENDING（核心因此不沾 webapi 依赖）
    inject: ClassVar[list[str]] = ["comm", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.comm_api
        self.ctx: Context = ctx

    async def init(self) -> object:
        """挂通道接口与健康指标（依赖集由 fiber 保证就绪，无需轮询）。

        路由注册绑定调用方 fiber：本服务卸载 / 重载时自动撤销重挂；健康指标
        句柄作为清理函数返回，随 fiber 卸载注销。
        """
        comm = cast("CommService", self.ctx.get("comm"))
        api = self.ctx.webapi
        api.post("/api/comm/ingest", comm.handle_ingest, kind="api", name="comm.ingest")
        api.post("/api/comm/poll", comm.handle_poll, kind="api", name="comm.poll")
        api.post("/api/comm/ack", comm.handle_ack, kind="api", name="comm.ack")
        return api.add_health_provider("comm", comm.metrics)


__all__: list[str] = ["CommApiService"]
