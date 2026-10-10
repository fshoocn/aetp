"""webapi 插件入口：管理 WebApiService 生命周期并自动加载插件管理 API。

使用方式（在 ``master/main.py`` 里）::

    from common.plugins.appconfig import AppConfigPlugin
    from common.plugins.webapi import WebApiPlugin

    await ctx.plugin(AppConfigPlugin, {"path": "master/config.ini"})
    await ctx.plugin(WebApiPlugin)

加载流程：

1. 构造 :class:`~common.plugins.webapi.uvicorn_web_service.UvicornWebApiService`
   （**构造即注册为 ``ctx.webapi``**）；
2. ``init()`` 注册内置自省接口并启动 uvicorn；
3. 卸载时由 effect 调用 :meth:`_shutdown` 停止服务器。

插件管理 API（:class:`~common.plugins.webapi.plugin_manager_plugin.PluginManagerPlugin`）
由**入口**在 webapi 就绪后单独加载——不在本插件内嵌加载，避免「webapi 尚未 ACTIVE
就加载依赖方」触发二轮加载、``ctx.plugins`` 短暂不可用的时序窗口。

``await ctx.plugin(...)`` 返回时 ``init()`` 已经跑完、端口已经在监听
（``start()`` 会阻塞到就绪），因此 ``ctx.webapi`` 对后续插件**立刻可用**，
不需要「就绪事件」之类的同步原语。要确认端口状态，直接用
``ctx.webapi.is_running()``。

UI 是可拆卸的上层：SPA 静态资源、页面回退与 ``register_ui`` 都在
``master.webui`` 插件里；不加载它就是纯 API 服务。

为什么不把 ``provide`` 写在插件类上
-----------------------------------

服务名在 cordis 里**全局唯一**，同名重复注册会抛
``RuntimeError: service "webapi" has been registered``。因此职责这样划分：

* **本类**：普通插件类（不是 ``Service``），靠 ``name`` 被 ``ctx.plugin()``
    加载，负责 Web 服务及内置管理 API 的生命周期；
* **后端**：``Service`` 子类，``provide = "webapi"``，成为 ``ctx.webapi``。
  而且**只有它**能拿到调用方 fiber（cordis 的影子上下文只作用于 provide
  载体），所以路由的自动清理实现在那里。
"""

from __future__ import annotations

from typing import ClassVar

from cordis_port import Context
from starlette.requests import Request

from .config import WebApiConfig
from .interface import WebApiService
from .uvicorn_web_service import UvicornWebApiService


class WebApiPlugin:
    """webapi 插件：依赖 ``ctx.appconfig`` 并读取 ``[web]`` 配置节。

    需要「web 就绪」这个时机的插件，直接声明 ``inject = ["webapi"]`` ——
    cordis 会按依赖顺序加载，等到它执行 ``__init__`` / ``init`` 时端口已经在
    监听，``ctx.webapi`` 可以直接用。要确认端口状态就用
    ``ctx.webapi.is_running()``；不声明依赖的插件不该关心 web 的时序，
    靠事件去「凑时序」是脆弱的。

    属性:
        service: 本插件持有的 :class:`WebApiService` 实现。
    """

    name: ClassVar[str] = "webapi"
    inject: ClassVar[list[str]] = ["appconfig"]

    def __init__(self, ctx: Context, _config: object | None = None) -> None:
        if _config is not None:
            raise TypeError("WebApiPlugin 配置请写入 config.ini 的 [web] 节")
        self.ctx: Context = ctx
        self.config: WebApiConfig = WebApiConfig.from_ini(
            ctx.appconfig.section("web").to_dict()
        )
        self.service: WebApiService = UvicornWebApiService(ctx, self.config)

    # cordis 会在插件实例构造完成后调用 init
    async def init(self) -> None:
        """启动 Web 服务并加载内置插件管理 API。"""
        self._register_builtins()
        self.service.start()

        address: str = self.service.address() or self.config.url()
        self.ctx.logger.info("web 服务已启动：%s", address)

        # 「停止服务器」的清理函数：effect 的**返回值**在 fiber 卸载时被调用
        # （注意是 lambda: self._shutdown，不是 lambda: self._shutdown()）。
        # 实测卸载顺序：业务插件的路由清理 → 停服务 → 内置路由清理。
        # 业务插件先清理并非「后注册先销毁」，而是 web fiber 进入 UNLOADING 时
        # 会先 notify 依赖方，它们的卸载任务先排入事件循环（见 cordis _update_state）。
        self.ctx.fiber.effect(lambda: self._shutdown, "plugins.webapi.shutdown")

    # -- 内置接口（属于本插件，随它一同卸载） --------------------------------
    def _register_builtins(self) -> None:
        service = self.service

        def list_routes(_: Request) -> dict[str, object]:
            """列出全部已注册路由（供排查「某个插件没挂上」的问题）。"""
            return {
                "address": service.address(),
                "count": len(service.routes()),
                "routes": [
                    {
                        "path": record.path,
                        "kind": record.kind,
                        "methods": sorted(record.methods),
                        "owner": record.owner,
                        "name": record.name,
                    }
                    for record in service.routes()
                ],
            }

        service.get(
            "/api/web/routes",
            list_routes,
            kind="api",
            name="web.routes",
        )

    # -- 卸载 -----------------------------------------------------------------
    def _shutdown(self) -> None:
        """停止 HTTP 服务并清空服务状态。

        由 effect 触发，因此 ``fiber.dispose()`` / ``fiber.restart()`` 都会走到。
        """
        try:
            self.service.stop()
        finally:
            self.ctx.logger.info("web 服务已停止")

    @property
    def address(self) -> str | None:
        """访问地址（未启动时为 ``None``）。"""
        return self.service.address()


__all__: list[str] = ["WebApiPlugin"]

