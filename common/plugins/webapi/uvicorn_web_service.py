"""uvicorn 后端 —— :class:`~common.plugins.webapi.interface.WebApiService` 的默认实现。

**所有 uvicorn 相关的代码都在本文件**：

* :class:`UvicornHost` —— 把 uvicorn 服务器挂在**宿主事件循环**上（单循环）；
* :class:`UvicornWebApiService` —— 把路由 / 响应拼成一个 WebApiService，
  并以 cordis ``Service``（``provide = "webapi"``）的身份注册为 ``ctx.webapi``。

换服务器（hypercorn / daphne …）时只需要换这个文件：新增一个同接口的实现，
再改 :mod:`common.plugins.webapi.plugin` 构造里那一行。接口
:class:`~common.plugins.webapi.interface.WebApiService` 与所有业务插件都保持不动。

为什么挂在宿主循环上而不是独立线程？
----------------------------------

``node_runtime.run_node`` 的主循环在 ``await stop.wait()`` 期间**全程存活**，
uvicorn 完全可以直接跑在它上面。历史版本用独立线程的真实原因是 uvicorn 在
主线程会自己接管 ``SIGINT``/``SIGTERM``（``capture_signals``），与
``node_runtime._install_stop_handler`` 的处理器互相覆盖、Ctrl+C 可能被截胡；
:class:`_EmbeddedServer` 显式禁用自带信号捕获后这个约束即消失：

* **单循环**——不存在跨线程的字典 / 事件访问，也没有线程 join 的关停互锁；
* 业务 handler 与管理面同栈运行，不再需要跨线程桥接（``run_from_web_thread``
  已删除）；
* 信号统一由 ``node_runtime`` 处理，Ctrl+C 语义不受 uvicorn 影响。

启动/停止时序（与 cordis 的 effect 生命周期对齐）::

    effect 创建 → 建 Starlette app → attach 路由 → 起 serve 任务 → 等 started
                                                            ↓ 超时/失败
                                                        抛异常 → 插件加载失败
    effect 销毁 → should_exit=True → await serve 任务 → detach 路由
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from pathlib import Path
from typing import cast

import uvicorn
from cordis_port import Context, Fiber, Service
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import AuthContext, AuthMiddleware, SharedSecretVerifier
from .config import WebApiConfig
from .interface import WebApiService
from .router import (
    RouteError,
    RouteRegistry,
    injectable_params,
    normalize_methods,
    normalize_path,
    param_converters,
    parse_path_params,
)
from .types import (
    RouteHandler,
    RouteKind,
    RouteRecord,
    RouteRegistration,
    StreamContent,
)


class ServerError(RuntimeError):
    """服务器启动或停止失败。"""


#: Vite 构建产物（文件名带内容 hash）的挂载前缀——内容变则文件名变，可安全长缓存
_HASHED_PREFIXES: tuple[str, ...] = ("/ui-static/assets/",)
#: 其余未 hash 的静态挂载前缀（插件 UI 资源随插件更新，须回源校验）
_STATIC_PREFIXES: tuple[str, ...] = ("/ui-static/", "/plugin-ui/")


class _SecurityHeadersMiddleware:
    """为所有响应补响应头：安全（``X-Content-Type-Options``）与缓存策略。

    缓存策略——静态响应原先不带 ``Cache-Control``，浏览器会按启发式
    （Last-Modified 的 10%）缓存，导致重建后**首次导航仍用旧产物**（如
    弹窗缺新字段），刷新才回源更新。现改为：HTML 一律 ``no-cache``
    （每次导航回源校验，命中则 304）；带内容 hash 的 Vite 构建产物
    ``immutable`` 长缓存；其余未 hash 静态（插件 UI 资源等）``no-cache``。

    纯 ASGI 中间件：只改响应头，不动响应体；静态资源（插件 UI、
    构建产物）与 API 响应统一受益。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def sender(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                cache_control = _cache_control(
                    scope.get("path", ""), headers.get("content-type", "")
                )
                if cache_control is not None:
                    headers["Cache-Control"] = cache_control
            await send(message)

        await self.app(scope, receive, sender)


def _cache_control(path: str, content_type: str) -> str | None:
    """按路径 / 内容类型给出缓存策略（``None`` = 不干预，如 API 响应）。"""
    if content_type.startswith("text/html"):
        return "no-cache"
    if any(path.startswith(prefix) for prefix in _HASHED_PREFIXES):
        return "public, max-age=31536000, immutable"
    if any(path.startswith(prefix) for prefix in _STATIC_PREFIXES):
        return "no-cache"
    return None


class _EmbeddedServer(uvicorn.Server):
    """嵌入宿主事件循环的 uvicorn 服务器：禁用自带信号捕获。

    信号统一由 ``node_runtime._install_stop_handler`` 处理；若不覆写，主线程
    下 ``capture_signals`` 会接管 ``SIGINT``/``SIGTERM``，Ctrl+C 被 uvicorn
    自己的 ``handle_exit`` 截胡，主循环的 ``stop.wait()`` 永远不返回。
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class UvicornHost:
    """在后台线程中托管一个 uvicorn 服务器。

    属性:
        app: Starlette 应用对象，路由表由 :class:`UvicornWebService` 动态维护。
        server: 底层 ``uvicorn.Server`` 实例（启动后才非 ``None``）。
    """

    def __init__(self, config: WebApiConfig, auth: AuthContext) -> None:
        self.config: WebApiConfig = config
        self.app: Starlette = Starlette()
        # 先加鉴权（栈内层），再加安全头 / CORS（外层）——鉴权的 401 响应
        # 也能带上 nosniff 与 CORS 头
        self.app.add_middleware(AuthMiddleware, auth=auth)
        self.app.add_middleware(_SecurityHeadersMiddleware)
        origins = [str(origin) for origin in config.cors_origins]
        if origins:
            # 跨源场景（如前端切换后端地址）需要 CORS；cors_origins 为空即关闭
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        self.server: _EmbeddedServer | None = None
        self._serve_task: asyncio.Task[None] | None = None

    async def _serve_guarded(self, server: uvicorn.Server) -> None:
        """跑 ``serve()``，把 uvicorn 启动失败的 ``SystemExit`` 折算成任务完成。

        uvicorn 在绑定失败时以 ``sys.exit(STARTUP_FAILURE)`` 结束 ``startup()``；
        线程模型下它只终止线程，单循环下却会穿出事件循环**直接杀进程**——
        这里吞掉 ``SystemExit``，让 :meth:`start` 以 :class:`ServerError`
        如实报告启动失败（``CancelledError`` 不在此列，照常向上传播）。
        """
        try:
            await server.serve()
        except SystemExit:
            pass

    # -- 启动 ----------------------------------------------------------------
    async def start(self) -> None:
        """启动服务器并等待就绪（或超时抛错）。

        抛出:
            ServerError: 端口被占用、绑定失败或超过 ``start_timeout`` 仍未就绪。
        """
        options = uvicorn.Config(
            self.app,
            host=self.config.host,
            port=int(self.config.port),
            log_level=self.config.log_level,
            access_log=self.config.access_log,
            # 不接管根 logger，避免与 cordis 的 logger 服务互相覆盖
            log_config=None,
            reload=False,
        )
        if self._serve_task is not None and not self._serve_task.done():
            return
        server = _EmbeddedServer(options)
        self.server = server
        task: asyncio.Task[None] = asyncio.create_task(self._serve_guarded(server))
        self._serve_task = task

        timeout = max(float(self.config.start_timeout), 0.1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.started:
                return
            if task.done():
                # serve 任务提前退出：端口占用或地址不可绑定
                self.server = None
                self._serve_task = None
                raise ServerError(
                    f"web 服务启动失败：{self.config.host}:{self.config.port} "
                    "（端口可能已被占用，或地址不可绑定）"
                )
            await asyncio.sleep(0.05)

        await self.stop()
        raise ServerError(
            f"web 服务未能在 {timeout:g} 秒内启动：{self.config.host}:{self.config.port}"
        )

    async def stop(self) -> None:
        """请求退出并等待 serve 任务结束（幂等，可在未启动时安全调用）。

        不用 ``wait_for``（超时会取消任务）：分两段 ``asyncio.wait`` 逐级
        强推（should_exit → force_exit → cancel），被取消时也确保任务回收。
        """
        server, task = self.server, self._serve_task
        self.server = None
        self._serve_task = None
        if server is None or task is None:
            return

        server.should_exit = True
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=max(float(self.config.start_timeout), 1.0)
            )
            if not done:
                # 没能在超时内退出：强推一次，避免端口长期被占
                server.force_exit = True
                done, _pending = await asyncio.wait({task}, timeout=2.0)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @property
    def is_running(self) -> bool:
        """服务器是否处于监听状态。"""
        task = self._serve_task
        return bool(
            self.server is not None and self.server.started and task is not None and not task.done()
        )

class UvicornWebApiService(Service[WebApiConfig], WebApiService):
    """基于 uvicorn 托管、Starlette 路由的 :class:`WebApiService` 实现。

    同时具备两个身份：

    * **cordis Service**（``provide = "webapi"``）—— 构造即注册为 ``ctx.webapi``；
    * **WebApiService 接口实现** —— 业务插件通过 ``ctx.webapi`` 拿到的就是本实例。

    两条**隐含契约**在这里兑现：

    1. ``route()`` 把注册项绑到**调用方 fiber** 的 effect 上，
       插件卸载时自动注销；
    2. 实例持有 ``ctx``，调用方身份从 :meth:`_caller_fiber` 推导。

    示例::

        @ctx.webapi.get("/api/ping", kind="api")
        def ping(request):
            return {"ok": True}
    """

    #: 服务名，其他插件用 ``inject = ["webapi"]`` 依赖它
    provide: str | None = "webapi"
    #: 插件名（本类也能被 ``ctx.plugin(UvicornWebApiService, {...})`` 直接加载）
    name: str = "webapi"
    #: 配置模型，供 cordis 校验 ``ctx.plugin(..., {...})`` 传入的配置
    Config: type[WebApiConfig] = WebApiConfig

    def __init__(self, ctx: Context, config: WebApiConfig | None = None) -> None:
        # 注意 MRO：基类是 (Service, WebApiService)，这里**显式**调用 Service 的构造，
        # 而不是 super().__init__ —— super 会先碰到抽象的 WebApiService.__init__
        # （只有一句 docstring，什么都不做），导致服务注册被跳过。
        cast(type[Service[WebApiConfig]], Service).__init__(self, ctx)

        self.config: WebApiConfig = (
            config if isinstance(config, WebApiConfig) else WebApiConfig()
        )
        self._registry: RouteRegistry = RouteRegistry()
        self._host: UvicornHost | None = None
        self._provided_ctx: Context = ctx
        self._fallback: Callable[[Request], Response | None] | None = None
        #: 鉴权接缝：默认共享密钥验证器，用户管理插件可替换（见 :mod:`.auth`）
        self._auth: AuthContext = AuthContext(SharedSecretVerifier(self.config))

    @property
    def auth(self) -> AuthContext:
        """鉴权接缝：持有可替换的验证器（默认共享密钥）。"""
        return self._auth

    # ---------------------------------------------------------- cordis 集成
    @property
    def fiber(self) -> Fiber:
        """提供本服务的 fiber（即 web 插件自身的 fiber）。

        可用于 ``ctx.web.fiber.restart()`` 重启服务，或读取 ``state``/``uid`` 做诊断。
        注意与 ``ctx.fiber``（当前插件的 fiber）区分。
        """
        return self._provided_ctx.fiber

    @property
    def provided_ctx(self) -> Context:
        """提供方上下文（web 插件自己的 ctx）。

        服务方法内读到的 ``self.ctx`` 是「调用方上下文」，需要访问服务自身所在
        上下文（例如派发全局事件、读取自身配置层）时请用本属性。
        """
        return self._provided_ctx

    def _caller_ctx(self) -> Context:
        """取本次调用的「使用点」上下文（即调用方插件自己的 ctx）。

        原理：cordis 调用服务方法时，会把 ``self.ctx`` 换成一个「以调用方上下文
        为原型、并把本服务登记为定义点」的影子上下文。因此这里读到的
        ``self.ctx`` 代表**谁在调用**，而不是「谁提供了 web 服务」。

        直接构造服务实例（如 web 插件自己注册内置路由）时不存在影子上下文，
        ``self.ctx`` 就是构造时传入的上下文，此时返回的是 web 插件自身。
        """
        return self.ctx

    def _caller_fiber(self) -> Fiber:
        """取调用方插件的 fiber，用于把注册项绑定到它的生命周期上。"""
        ctx = self._caller_ctx()
        fiber = getattr(ctx, "fiber", None)
        return fiber if fiber is not None else self._provided_ctx.fiber

    # ------------------------------------------------------------ 服务器生命周期
    async def start(self) -> None:
        """启动 uvicorn（等待就绪；失败抛 :class:`ServerError`）。

        在此之前注册的路由会被一次性挂载；之后注册的路由随 :meth:`route` 立即生效。
        """
        host = UvicornHost(self.config, self.auth)
        host.app.state.web_service = self
        # 先把 app 交给路由表，再启动：这样启动期间注册的路由也能被匹配到
        self._attach_app(host.app)
        self._attach_server(host)
        # 错误信封统一为 {"error": ...}（前端 request() 只读 error 字段）：
        # 404 走回退链（set_fallback 的处理器如 webui 的 SPA 回退优先，返回
        # None 则落到 JSON 404）；其余 HTTPException 与未处理异常各自兜底。
        host.app.add_exception_handler(404, self._not_found)
        host.app.add_exception_handler(HTTPException, self._http_exception)
        host.app.add_exception_handler(500, self._internal_error)
        await host.start()

    async def stop(self) -> None:
        """停止 uvicorn 并清空服务状态（幂等，未启动时可安全调用）。"""
        host = self._host
        try:
            if host is not None:
                await host.stop()
        finally:
            self._detach_app()
            self._detach_server()
            self._dispose()

    # -------------------------------------------------------------- 路由注册
    def route(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        methods: str | Sequence[str] | None = None,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        resolved_path = normalize_path(path)
        resolved_methods = normalize_methods(methods)
        params = tuple(parse_path_params(resolved_path))
        caller_fiber = self._caller_fiber()

        def register(target: RouteHandler) -> RouteHandler:
            if not callable(target):
                raise RouteError(f"handler 必须是可调用对象，收到 {type(target).__name__}")
            record = RouteRecord(
                path=resolved_path,
                kind=kind,
                methods=resolved_methods,
                handler=target,
                endpoint=None,
                name=name,
                owner=_fiber_name(caller_fiber),
                params=injectable_params(target, params),
                converters=param_converters(target, params),
            )
            self._registry.add(record, owner_fiber=caller_fiber)
            return target

        if handler is None:
            return register
        return register(handler)

    def get(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        return self.route(path, handler, methods="GET", kind=kind, name=name)

    def post(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        return self.route(path, handler, methods="POST", kind=kind, name=name)

    def put(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        return self.route(path, handler, methods="PUT", kind=kind, name=name)

    def patch(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        return self.route(path, handler, methods="PATCH", kind=kind, name=name)

    def delete(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        kind: RouteKind = "route",
        name: str | None = None,
    ) -> RouteRegistration:
        return self.route(path, handler, methods="DELETE", kind=kind, name=name)

    def api(
        self,
        path: str,
        handler: RouteHandler | None = None,
        *,
        methods: str | Sequence[str] | None = None,
        name: str | None = None,
    ) -> RouteRegistration:
        return self.route(path, handler, methods=methods, kind="api", name=name)

    def static(
        self,
        path: str,
        directory: str | Path,
        *,
        name: str | None = None,
        html: bool = False,
    ) -> RouteRecord:
        resolved_path = normalize_path(path, allow_root=True)
        resolved_dir = Path(directory).expanduser()
        if not resolved_dir.is_absolute():
            resolved_dir = (Path.cwd() / resolved_dir).resolve()
        caller_fiber = self._caller_fiber()
        record = RouteRecord(
            path=resolved_path,
            kind="static",
            methods=frozenset({"GET"}),
            handler=None,
            endpoint=None,
            name=name,
            owner=_fiber_name(caller_fiber),
            directory=str(resolved_dir),
            html=html,
        )
        self._registry.add(record, owner_fiber=caller_fiber)
        return record

    def set_fallback(
        self, handler: Callable[[Request], Response | None] | None
    ) -> None:
        """设置（或清除）404 回退处理器（后设置者覆盖）。"""
        self._fallback = handler

    async def _not_found(self, request: Request, exc: Exception) -> Response:
        """404 总入口：先给回退处理器机会，否则返回 JSON 404（统一 ``error`` 信封，
        自定义 detail 不被吞掉）。"""
        handler = self._fallback
        if handler is not None:
            response = handler(request)
            if isinstance(response, Response):
                return response
        detail = str(exc.detail) if isinstance(exc, HTTPException) else "Not Found"
        return JSONResponse({"error": detail}, status_code=404)

    async def _http_exception(self, _request: Request, exc: Exception) -> Response:
        """非 404 的 ``HTTPException``：统一 ``error`` 信封（starlette 默认是纯文本）。"""
        assert isinstance(exc, HTTPException)
        if exc.status_code in {204, 304}:
            return Response(status_code=exc.status_code, headers=exc.headers)
        return JSONResponse(
            {"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers
        )

    async def _internal_error(self, _request: Request, exc: Exception) -> Response:
        """未处理异常：统一 ``error`` 信封且不泄漏内部细节（traceback 仍上抛给 uvicorn 记录）。"""
        del exc
        return JSONResponse({"error": "服务器内部错误"}, status_code=500)

    # -------------------------------------------------------------- 路由管理
    def remove(self, path: str) -> bool:
        return self._registry.remove(normalize_path(path))

    def routes(self) -> list[RouteRecord]:
        return [*self._registry, *self._registry.statics.values()]

    def has_route(self, path: str) -> bool:
        return self._registry.get(normalize_path(path)) is not None

    # -------------------------------------------------------------- 响应构造
    #
    # 这几个方法是**实例方法**（接口 ``WebService`` 如此声明）；虽然实现不依赖
    # ``self``，也不改成 ``@staticmethod`` —— 覆盖签名必须与基类一致，
    # 否则类型检查会报「以不兼容的方式替代」。
    def json(
        self,
        value: object,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> JSONResponse:
        return JSONResponse(value, status_code=status_code, headers=dict(headers or {}))

    def html(
        self,
        value: str,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> HTMLResponse:
        return HTMLResponse(value, status_code=status_code, headers=dict(headers or {}))

    def text(
        self,
        value: str,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> PlainTextResponse:
        return PlainTextResponse(value, status_code=status_code, headers=dict(headers or {}))

    def redirect(self, url: str, *, status_code: int = 307) -> RedirectResponse:
        return RedirectResponse(url, status_code=status_code)

    def file(
        self,
        path: str | Path,
        *,
        filename: str | None = None,
        media_type: str | None = None,
    ) -> FileResponse:
        return FileResponse(str(path), filename=filename, media_type=media_type)

    def stream(
        self,
        content: StreamContent,
        *,
        media_type: str | None = None,
        status_code: int = 200,
    ) -> StreamingResponse:
        return StreamingResponse(content, media_type=media_type, status_code=status_code)

    # ------------------------------------------------------------ 运行时查询
    def url_for(self, name: str, **params: object) -> str:
        record = next(
            (
                item
                for item in self.routes()
                if item.name == name or item.path == normalize_path(name)
            ),
            None,
        )
        if record is None:
            raise RouteError(f"未找到名为 {name!r} 的路由")
        path = record.path
        for param in parse_path_params(path):
            if param not in params:
                raise RouteError(f"路由 {path!r} 需要路径参数 {param!r}")
            path = path.replace("{" + param + "}", str(params[param]))
            # 兼容 ``{param:path}`` 写法
            index = path.find("{" + param + ":")
            if index != -1:
                end = path.find("}", index)
                path = path[:index] + str(params[param]) + path[end + 1 :]
        return path

    def is_running(self) -> bool:
        return bool(self._host is not None and self._host.is_running)

    def address(self) -> str | None:
        if not self.is_running():
            return None
        return self.config.url()

    # -------------------------------------------------------------- 内部对接
    def _attach_server(self, host: UvicornHost) -> None:
        self._host = host

    def _detach_server(self) -> None:
        self._host = None

    def _attach_app(self, app: Starlette) -> None:
        """把 Starlette 应用交给路由表（此后注册的路由会立即挂载）。"""
        self._registry.attach_app(app)

    def _detach_app(self) -> None:
        self._registry.detach_app()

    def _dispose(self) -> None:
        """清空本服务持有的全部状态。"""
        self._registry.clear()
        self._fallback = None
        self._host = None

    def __repr__(self) -> str:
        state = "running" if self.is_running() else "stopped"
        return f"UvicornWebApiService({state}, {len(self._registry)} routes)"


def _fiber_name(fiber: Fiber | None) -> str:
    """取 fiber 名用于诊断（``RouteRecord.owner``）；无 fiber 时回退 ``"root"``。"""
    return getattr(fiber, "name", None) or "root"


__all__: list[str] = ["ServerError", "UvicornHost", "UvicornWebApiService"]
