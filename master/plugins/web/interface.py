"""Web 服务的**接口**：只声明「web 服务长什么样」，不含任何实现。

业务插件依赖的就是这个契约（通过 ``ctx.web`` 拿到的正是它的实现）。
换底层服务器 / 框架时本模块**不动**，业务插件一行都不用改 —— 新后端只要
实现 :class:`WebService` 即可。

设计约束（都是为了「后端可整体替换」）::

    1. 不出现 uvicorn / hypercorn 等服务器名 —— 服务器只活在后端实现文件里；
    2. 不暴露框架内部对象 —— 不返回 Starlette 的 Route/Mount，
       只返回本包自己的 :class:`~plugins.web.types.RouteRecord`；
    3. 响应构造器只依赖 ``starlette.responses`` —— 它们是 ASGI 无关的纯对象，
       换任何 ASGI 服务器都不受影响。

模块结构::

    interface.py            本文件：接口 WebService（换后端时不动）
    uvicorn_web_service.py  uvicorn 后端：UvicornWebService + UvicornHost
    router.py               路由表与 Starlette 挂载
    layout.py               页面布局与导航栏渲染
    responses.py            handler 返回值 → HTTP 响应
    config.py               配置模型（标准 Schema）
    types.py                纯数据结构
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path

from cordis_port import Context, Fiber
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)

from .config import WebConfig
from .types import (
    RouteHandler,
    RouteKind,
    RouteRecord,
    RouteRegistration,
    StreamContent,
    UiContribution,
    UiFormat,
    UiKind,
)


class WebService(ABC):
    """web 服务的抽象接口。

    覆盖路由、插件 UI contributions、HTTP 响应和运行时查询能力：

    =========================  =============================================
    能力                        方法
    =========================  =============================================
    路由注册                    :meth:`route` / :meth:`get` / :meth:`post` /
                                :meth:`put` / :meth:`patch` / :meth:`delete` /
                                :meth:`api` / :meth:`static`
    插件 UI                     :meth:`register_ui` / :meth:`ui_contributions`
    路由管理                    :meth:`remove` / :meth:`routes` / :meth:`has_route`
    响应构造                    :meth:`json` / :meth:`html` / :meth:`text` /
                                :meth:`redirect` / :meth:`file` / :meth:`stream`
    运行时查询                  :meth:`url_for` / :meth:`is_running` / :meth:`address`
    =========================  =============================================

    实现方还需满足两条**隐含契约**（无法用 ``abstractmethod`` 表达，但必须做到）：

        * :meth:`register_ui` / :meth:`route` 注册的条目要绑定到**调用方插件的 fiber**，
            插件卸载时自动注销；
    * 实例需持有 ``ctx``，因为调用方身份正是从它推导出来的。
    """

    # ---------------------------------------------------------------- 构造
    @abstractmethod
    def __init__(self, ctx: Context, config: WebConfig) -> None:
        """构造服务。

        参数:
            ctx: 提供方上下文。用于识别「谁在注册」（见类文档的隐含契约）。
            config: 已校验过的配置实例。

        注意:
            具体实现可放宽签名（Python 不校验），但 ``ctx`` 与 ``config``
            是所有实现都应接受的最小集合。
        """

    # ---------------------------------------------------------- cordis 集成
    @property
    @abstractmethod
    def fiber(self) -> Fiber:
        """提供本服务的 fiber（即 web 插件自身的 fiber）。

        可用于 ``ctx.web.fiber.restart()`` 重启服务，或读取 ``state`` 做诊断。
        """

    @property
    @abstractmethod
    def provided_ctx(self) -> Context:
        """提供方上下文（web 插件自己的 ctx）。

        服务方法内读到的 ``ctx`` 是**调用方**上下文，需要访问自身配置层或
        派发全局事件时用本属性。
        """

    # ------------------------------------------------------------ 生命周期
    @abstractmethod
    def start(self) -> None:
        """启动服务器（阻塞到就绪；失败抛运行时错误）。

        由 web 插件的 ``init()`` 调用。此调用之前注册的路由会一次性挂载，
        之后注册的路由应立即生效（无需重启）。
        """

    @abstractmethod
    def stop(self) -> None:
        """停止服务器并清空运行态（幂等，未启动时可安全调用）。

        由 web 插件登记的 effect 调用，因此 ``fiber.dispose()`` /
        ``fiber.restart()`` 都会走到这里。
        """

    # -------------------------------------------------------------- 路由注册
    @abstractmethod
    def route(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        methods: str | Sequence[str] | None = None,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        """注册路由（装饰器 / 显式调用两用）。

        参数:
            path: 路由路径，支持 ``{name}`` 形式的路径参数。
            handler: 处理函数；省略时返回装饰器，传入时返回它本身。
            methods: 允许的 HTTP 方法，缺省 ``GET``。
            kind: 路由种类，决定返回值如何转成响应。
            name: 路由名，供 :meth:`url_for` 反向生成 URL。
        示例::

            @ctx.web.route("/ping")
            def ping(request):
                return "pong"

            ctx.web.route("/ping", ping, methods="POST")
        """

    @abstractmethod
    def get(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        """注册 ``GET`` 路由。"""

    @abstractmethod
    def post(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        """注册 ``POST`` 路由。"""

    @abstractmethod
    def put(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        """注册 ``PUT`` 路由。"""

    @abstractmethod
    def patch(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        """注册 ``PATCH`` 路由。"""

    @abstractmethod
    def delete(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        """注册 ``DELETE`` 路由。"""

    @abstractmethod
    def api(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        methods: str | Sequence[str] | None = None,
        name: str | None = None,
    ) -> RouteRegistration:
        """注册 API 路由：返回值一律按 JSON 处理。"""

    @abstractmethod
    def static(
        self,
        path: str,
        directory: str | Path,
        *,
        name: str | None = None,
        html: bool = False,
    ) -> RouteRecord:
        """挂载静态目录。

        参数:
            path: 挂载前缀，例如 ``/assets/my-plugin``。
            directory: 本地目录。
            name: 路由名。
            html: 是否开启 HTML 模式（目录下有 ``index.html`` 时自动返回）。

        返回:
            登记的路由记录。
        """

    @abstractmethod
    def register_ui(
        self,
        contribution_id: str,
        *,
        kind: UiKind,
        format: UiFormat,
        resource_root: str | Path,
        entry: str,
        path: str | None = None,
        title: str | None = None,
        menu_group: str | None = None,
        menu_icon: str | None = None,
        menu_order: float = 0,
        target: str | None = None,
        order: float = 0,
    ) -> UiContribution:
        """注册插件页面、插槽组件或 JS 扩展，并绑定到调用插件的生命周期。"""

    @abstractmethod
    def ui_contributions(self) -> list[UiContribution]:
        """返回插件前端扩展清单。"""

    # -------------------------------------------------------------- 路由管理
    @abstractmethod
    def remove(self, path: str) -> bool:
        """注销该路径下的**全部**路由（含静态目录），返回是否确实删掉了内容。"""

    @abstractmethod
    def routes(self) -> list[RouteRecord]:
        """列出全部已注册路由（按注册顺序，同路径的多方法记录都会列出）。"""

    @abstractmethod
    def has_route(self, path: str) -> bool:
        """判断某路径是否已注册。"""

    # -------------------------------------------------------------- 响应构造
    @abstractmethod
    def json(
        self,
        value: object,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponse:
        """构造 JSON 响应。"""

    @abstractmethod
    def html(
        self,
        value: str,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> HTMLResponse:
        """构造 HTML 响应（不套布局，用于片段接口）。"""

    @abstractmethod
    def text(
        self,
        value: str,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> PlainTextResponse:
        """构造纯文本响应。"""

    @abstractmethod
    def redirect(self, url: str, *, status_code: int = 307) -> RedirectResponse:
        """构造重定向响应。"""

    @abstractmethod
    def file(
        self,
        path: str | Path,
        *,
        filename: str | None = None,
        media_type: str | None = None,
    ) -> FileResponse:
        """构造文件下载响应。"""

    @abstractmethod
    def stream(
        self,
        content: StreamContent,
        *,
        media_type: str | None = None,
        status_code: int = 200,
    ) -> StreamingResponse:
        """构造流式响应（``content`` 为同步/异步生成器）。"""

    # ------------------------------------------------------------ 运行时查询
    @abstractmethod
    def url_for(self, name: str, **params: object) -> str:
        """按路由名反向生成 URL。

        参数:
            name: 注册时的 ``name``（或路由路径本身）。
            params: 路径参数值。

        返回:
            生成的 URL 路径。

        抛出:
            LookupError: 找不到该名字，或缺少必需的路径参数。
        """

    @abstractmethod
    def is_running(self) -> bool:
        """Web 服务器当前是否在监听。"""

    @abstractmethod
    def address(self) -> str | None:
        """返回形如 ``http://127.0.0.1:8080`` 的访问地址；未启动时返回 ``None``。"""


__all__: list[str] = ["WebService"]
