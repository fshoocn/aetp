"""heartbeat（从模块）的自省 API 服务（``ctx.heartbeat_api``）。

心跳核心（HeartbeatPlugin）承载心跳上报 / 状态机 / 能力同步，不沾 Web
依赖；自省接口独立成本服务、``inject = ["heartbeat", "webapi"]``
**注入式挂载**——两者就位 cordis 才装载并挂路由（依赖缺席安静 PENDING，
核心 / webapi 换装、重载随依赖纪元自动重挂）。``GET /api/heartbeat/status``
返回值带 ``extensions`` 展示扩展槽与 ``capabilities`` 能力信封列表，其他插件
可经 :data:`STATUS_EVENT` / ``heartbeat/collect-capabilities`` 事件插入自家数据。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from cordis_port import Context, EventDispatchError, Service
from starlette.requests import Request

from common.capabilities import capabilities_digest, collect_capabilities

if TYPE_CHECKING:  # 运行期无环：plugin 导入本模块装载子服务
    from .plugin import HeartbeatPlugin

#: 状态接口的扩展收集事件：``GET /api/heartbeat/status`` 构造好响应后派发它，
#: 并把**响应 dict** 传给监听器；其他插件把自家数据写进
#: ``response["extensions"]["<插件名>"]`` 即会出现在返回值里。
STATUS_EVENT = "heartbeat/collect-status"


class HeartbeatApiService(Service[None]):
    """heartbeat 自省 API 服务：挂 ``GET /api/heartbeat/status`` 与健康指标。"""

    #: 附属服务标记：本类是 ``heartbeat`` 插件的界面层载体之一，不是独立插件——
    #: 插件清单（builtin_specs）据此不单列，随宿主插件 heartbeat 一起呈现
    service_of: ClassVar[str] = "heartbeat"
    #: 服务名（即 ``ctx.heartbeat_api``）
    provide: str | None = "heartbeat_api"
    #: fiber 名（路由归属与诊断用）
    name: str = "heartbeat_api"
    #: 自省 API 的依赖集 = 自家核心 ``heartbeat`` + Web 服务 ``webapi``：两者
    #: 就位才装载，依赖缺席安静 PENDING（核心因此不沾 webapi 依赖）
    inject: ClassVar[list[str]] = ["heartbeat", "webapi"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        super().__init__(ctx)  # 最先调用：注册 ctx.heartbeat_api
        self.ctx: Context = ctx

    async def init(self) -> object:
        """挂自省接口与健康指标（依赖集由 fiber 保证就绪，无需轮询）。

        路由注册绑定调用方 fiber：本服务卸载 / 重载时自动撤销重挂；健康指标
        句柄作为清理函数返回，随 fiber 卸载注销。
        """
        api = self.ctx.webapi
        api.get(
            "/api/heartbeat/status",
            self._status,
            kind="api",
            name="heartbeat.status",
        )
        return api.add_health_provider("heartbeat", self._core.health_metrics)

    @property
    def _core(self) -> HeartbeatPlugin:
        return cast("HeartbeatPlugin", self.ctx.get("heartbeat"))

    # -- 自省接口 -----------------------------------------------------------
    async def _status(self, _request: Request) -> dict[str, object]:
        """``GET /api/heartbeat/status``：状态快照 + 其他插件的扩展数据。

        扩展契约：派发 :data:`STATUS_EVENT` 并把响应 dict 传给监听器，
        其他插件把自家数据写进 ``response["extensions"]["<插件名>"]``
        （核心字段只读，避免撞名）；监听器同步 / 异步均可，**并发**派发，
        某个贡献者出错不影响本接口 —— 错误进 ``response["extension_errors"]``
        （恒在，无错时为空列表；非法能力条目的问题也汇入这里），其余贡献
        照常返回。能力信封列表见 ``response["capabilities"]`` / ``caps_digest``。
        """
        response = self._core.monitor.snapshot().to_dict()
        extensions: dict[str, object] = {}
        errors: list[str] = []
        response["extensions"] = extensions
        response["extension_errors"] = errors
        entries, problems = await collect_capabilities(self.ctx)
        response["capabilities"] = entries
        response["caps_digest"] = capabilities_digest(entries)
        errors.extend(problems)
        try:
            await self.ctx.parallel(STATUS_EVENT, response)
        except EventDispatchError as exc:
            errors.extend(f"{type(error).__name__}: {error}" for error in exc.errors)
        return response


__all__: list[str] = ["STATUS_EVENT", "HeartbeatApiService"]
