"""Web 插件入口：管理 WebService 生命周期并自动加载插件管理 API。

使用方式（在 ``master/main.py`` 里）::

    from plugins.web import WebPlugin

    await ctx.plugin(WebPlugin, {"port": 8080})

加载流程：

1. 构造 :class:`~plugins.web.uvicorn_web_service.UvicornWebService`（**构造即注册为
   ``ctx.web``**）；
2. ``init()`` 注册内置自省接口并启动 uvicorn；
3. 自动加载依赖 ``web`` 的插件管理 API；
4. 卸载时先清理管理 API，再由 effect 调用 :meth:`_shutdown` 停止服务器。

``await ctx.plugin(...)`` 返回时 ``init()`` 已经跑完、端口已经在监听
（``start()`` 会阻塞到就绪），因此 ``ctx.web`` 对后续插件**立刻可用**，
不需要「就绪事件」之类的同步原语。要确认端口状态，直接用
``ctx.web.is_running()``。

为什么不把 ``provide`` 写在插件类上
-----------------------------------

服务名在 cordis 里**全局唯一**，同名重复注册会抛
``RuntimeError: service "web" has been registered``。因此职责这样划分：

* **本类**：普通插件类（不是 ``Service``），靠 ``name`` 被 ``ctx.plugin()``
    加载，负责 Web 服务及内置管理 API 的生命周期；
* **后端**：``Service`` 子类，``provide = "web"``，成为 ``ctx.web``。
  而且**只有它**能拿到调用方 fiber（cordis 的影子上下文只作用于 provide
 载体），所以路由与 UI contribution 的自动清理都实现在那里。
"""

from __future__ import annotations

from typing import ClassVar

from cordis_port import Context
from starlette.requests import Request

from .config import WebConfig
from .interface import WebService
from .uvicorn_web_service import UvicornWebService


class WebPlugin:
    """web 插件。配置留空即用 :class:`WebConfig` 的默认值::

        {"host": "127.0.0.1", "port": 8080}

    需要「web 就绪」这个时机的插件，直接声明 ``inject = ["web"]`` —— cordis 会按
    依赖顺序加载，等到它执行 ``__init__`` / ``init`` 时端口已经在监听，``ctx.web``
    可以直接用。要确认端口状态就用 ``ctx.web.is_running()``；不声明依赖的插件
    不该关心 web 的时序，靠事件去「凑时序」是脆弱的。

    属性:
        service: 本插件持有的 :class:`WebService` 实现。
    """

    name: ClassVar[str] = "web"
    Config: ClassVar[type[WebConfig]] = WebConfig

    def __init__(self, ctx: Context, config: WebConfig | None = None) -> None:
        self.ctx: Context = ctx
        # 保存为属性：init() 在后面才被 cordis 调用，那时局部变量已失效
        self.config: WebConfig = config if isinstance(config, WebConfig) else WebConfig()
        self.service: WebService = UvicornWebService(ctx, self.config)

    # cordis 会在插件实例构造完成后调用 init
    async def init(self) -> None:
        """启动 Web 服务并加载内置插件管理 API。"""
        self._register_builtins()
        self.service.start()

        address: str = self.service.address() or self.config.url()
        self.ctx.logger.info("web 服务已启动：%s", address)

        # 「停止服务器」的清理函数：effect 的**返回值**在 fiber 卸载时被调用
        # （注意是 lambda: self._shutdown，不是 lambda: self._shutdown()）。
        # 实测卸载顺序：业务插件的路由/UI 清理 → 停服务 → 内置路由清理。
        # 业务插件先清理并非「后注册先销毁」，而是 web fiber 进入 UNLOADING 时
        # 会先 notify 依赖方，它们的卸载任务先排入事件循环（见 cordis _update_state）。
        self.ctx.fiber.effect(lambda: self._shutdown, "plugins.web.shutdown")

        from .plugin_manager_plugin import PluginManagerPlugin

        await self.ctx.plugin(
            PluginManagerPlugin,
            {"install_root": self.config.plugin_install_root},
        )

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

        def list_ui(_: Request) -> list[dict[str, str | float | None]]:
            """列出 Vue 宿主需要挂载的插件 UI contributions。"""
            return [item.to_dict() for item in service.ui_contributions()]

        service.get(
            "/api/web/routes",
            list_routes,
            kind="api",
            name="web.routes",
        )
        service.get(
            "/api/web/ui",
            list_ui,
            kind="api",
            name="web.ui",
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


__all__: list[str] = ["WebPlugin"]

