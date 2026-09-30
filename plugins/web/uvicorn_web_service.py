"""uvicorn 后端 —— :class:`~plugins.web.interface.WebService` 的默认实现。

**所有 uvicorn 相关的代码都在本文件**：

* :class:`UvicornHost` —— 把 Starlette 应用跑在一个后台线程里；
* :class:`UvicornWebService` —— 把路由 / 布局 / 响应拼成一个 WebService，
  并以 cordis ``Service``（``provide = "web"``）的身份注册为 ``ctx.web``。

换服务器（hypercorn / daphne …）时只需要换这个文件：新增一个同接口的实现，
再改 :mod:`plugins.web.plugin` 构造里那一行。接口
:class:`~plugins.web.interface.WebService` 与所有业务插件都保持不动。

为什么放在独立线程而不是复用调用方的 asyncio 循环？

* 主流程（``master/main.py``）是单次 ``asyncio.run``，插件加载完就结束，没有
  长期运行的循环可以挂载服务；
* 后台线程让「启动 web 服务」与「插件运行时」解耦：插件加载完成后服务仍在
  监听，退出进程时线程随之为 daemon 结束，不需要调用方额外管理生命周期；
* uvicorn 在非主线程会自动跳过信号注册（``capture_signals`` 检测线程），因此
  不会抢占 ``Ctrl+C``，主线程仍可正常中断。

启动/停止时序（与 cordis 的 effect 生命周期对齐）::

    effect 创建 → 建 Starlette app → attach 路由 → 起线程 → 等 started
                                                        ↓ 超时/失败
                                                    抛异常 → 插件加载失败
    effect 销毁 → should_exit=True → join 线程 → detach 路由

停止时的加锁策略（重要）：``UvicornHost.stop()`` 在锁内**只做快照**（取出
server/thread 并置空自己的字段），真正的 ``join()`` 放在锁外执行 —— 否则线程
若在退出过程中回调 ``is_running`` 之类的加锁方法就会死锁。
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cordis_port import Context, Fiber, Service
from starlette.applications import Starlette
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

from .config import WebConfig
from .interface import WebService
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
    UiContribution,
    UiFormat,
    UiKind,
)

if TYPE_CHECKING:
    # 仅供类型标注：uvicorn 仍在 ``start()`` 内按需导入，运行时导入面保持最小
    import uvicorn


class ServerError(RuntimeError):
    """服务器启动或停止失败。"""


class UvicornHost:
    """在后台线程中托管一个 uvicorn 服务器。

    属性:
        app: Starlette 应用对象，路由表由 :class:`UvicornWebService` 动态维护。
        server: 底层 ``uvicorn.Server`` 实例（启动后才非 ``None``）。
    """

    def __init__(self, config: WebConfig) -> None:
        self.config: WebConfig = config
        self.app: Starlette = Starlette()
        self.thread: threading.Thread | None = None
        self.server: uvicorn.Server | None = None
        self._lock = threading.Lock()
        self._installed_fallback: bool = False

    # -- 启动 ----------------------------------------------------------------
    def start(self) -> None:
        """启动服务器并阻塞到就绪（或超时抛错）。

        抛出:
            ServerError: 端口被占用、绑定失败或超过 ``start_timeout`` 仍未就绪。
        """
        import uvicorn

        self.install_fallback_index()

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
        with self._lock:
            if self.thread is not None and self.thread.is_alive():
                return
            server = uvicorn.Server(options)
            self.server = server
            self.thread = threading.Thread(
                target=server.run,
                name="aetp-web",
                daemon=True,
            )
            thread = self.thread
        thread.start()

        timeout = max(float(self.config.start_timeout), 0.1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.started:
                return
            if not thread.is_alive():
                # 线程提前退出：端口占用或地址不可绑定
                with self._lock:
                    self.server = None
                    self.thread = None
                raise ServerError(
                    f"web 服务启动失败：{self.config.host}:{self.config.port} "
                    "（端口可能已被占用，或地址不可绑定）"
                )
            time.sleep(0.05)

        self.stop()
        raise ServerError(
            f"web 服务未能在 {timeout:g} 秒内启动：{self.config.host}:{self.config.port}"
        )

    def stop(self) -> None:
        """请求退出并等待线程结束（幂等，可在未启动时安全调用）。

        锁内只做快照，``join`` 在锁外进行 —— 避免持锁等待线程退出而死锁。
        """
        with self._lock:
            server, thread = self.server, self.thread
            self.server = None
            self.thread = None
        if server is None:
            return

        server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(float(self.config.start_timeout), 1.0))
        if thread is not None and thread.is_alive():
            # 线程没能在超时内退出：强推一次，避免端口长期被占
            server.force_exit = True
            thread.join(timeout=2.0)

    @property
    def is_running(self) -> bool:
        """服务器是否处于监听状态。"""
        with self._lock:
            server, thread = self.server, self.thread
        return bool(
            server is not None and server.started and thread is not None and thread.is_alive()
        )

    # -- 兜底 404 页 ---------------------------------------------------------
    def install_fallback_index(self) -> None:
        """安装 SPA 路由回退及明确的 API/静态资源 404。

        页面导航 URL 返回构建后的 ``index.html``，而 API 和静态前缀保持真实 404，
        避免 SPA 回退吞掉拼错的 API 或缺失资源。

        仅安装一次。
        """
        if self._installed_fallback:
            return
        self._installed_fallback = True
        self.app.add_exception_handler(404, self._fallback_404)

    async def _fallback_404(self, request: Request, _exc: Exception) -> Response:
        """为 SPA 前端路由回退；API 与静态路径仍返回 404。"""
        del _exc
        path = request.url.path
        if path == "/api" or path.startswith("/api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if path == "/ui-static" or path.startswith("/ui-static/"):
            return PlainTextResponse("Not Found", status_code=404)
        if path == "/plugin-ui" or path.startswith("/plugin-ui/"):
            return PlainTextResponse("Not Found", status_code=404)
        if request.method not in {"GET", "HEAD"}:
            return PlainTextResponse("Not Found", status_code=404)

        ui_index = Path(request.app.state.ui_directory) / "index.html"
        if not ui_index.is_file():
            return JSONResponse(
                {"detail": "Vue UI is not built; run npm run build in plugins/web/frontend"},
                status_code=503,
            )
        if request.method == "HEAD":
            return Response(
                status_code=200,
                media_type="text/html",
                headers={"Content-Length": str(ui_index.stat().st_size)},
            )
        return FileResponse(ui_index)


class UvicornWebService(Service[WebConfig], WebService):
    """基于 uvicorn 托管、Starlette 路由的 :class:`WebService` 实现。

    同时具备两个身份：

    * **cordis Service**（``provide = "web"``）—— 构造即注册为 ``ctx.web``；
    * **WebService 接口实现** —— 业务插件通过 ``ctx.web`` 拿到的就是本实例。

    两条**隐含契约**在这里兑现：

    1. ``route()`` / ``register_ui()`` 把注册项绑到**调用方 fiber** 的 effect 上，
       插件卸载时自动注销；
    2. 实例持有 ``ctx``，调用方身份从 :meth:`_caller_fiber` 推导。

    示例::

        ctx.web.register_ui(
            "dashboard", kind="page", format="vue", resource_root=".../ui",
            entry="Dashboard.vue", path="/tests", title="测试管理",
        )
    """

    #: 服务名，其他插件用 ``inject = ["web"]`` 依赖它
    provide: str | None = "web"
    #: 插件名（本类也能被 ``ctx.plugin(UvicornWebService, {...})`` 直接加载）
    name: str = "web"
    #: 配置模型，供 cordis 校验 ``ctx.plugin(..., {...})`` 传入的配置
    Config: type[WebConfig] = WebConfig

    def __init__(self, ctx: Context, config: WebConfig | None = None) -> None:
        # 注意 MRO：基类是 (Service, WebService)，这里**显式**调用 Service 的构造，
        # 而不是 super().__init__ —— super 会先碰到抽象的 WebService.__init__
        # （只有一句 docstring，什么都不做），导致服务注册被跳过。
        cast(type[Service[WebConfig]], Service).__init__(self, ctx)

        self.config: WebConfig = config if isinstance(config, WebConfig) else WebConfig()
        self._registry: RouteRegistry = RouteRegistry()
        self._ui: dict[str, UiContribution] = {}
        self._host: UvicornHost | None = None
        self._provided_ctx: Context = ctx

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
    def start(self) -> None:
        """启动 uvicorn（阻塞到就绪；失败抛 :class:`ServerError`）。

        在此之前注册的路由会被一次性挂载；之后注册的路由随 :meth:`route` 立即生效。
        """
        host = UvicornHost(self.config)
        host.app.state.web_service = self
        # 先把 app 交给路由表，再启动：这样启动期间注册的路由也能被匹配到
        self._attach_app(host.app)
        self._attach_server(host)
        ui_directory = Path(self.config.ui_directory).expanduser().resolve()
        host.app.state.ui_directory = str(ui_directory)
        if ui_directory.is_dir():
            from starlette.staticfiles import StaticFiles

            host.app.mount(
                "/ui-static",
                app=StaticFiles(directory=str(ui_directory)),
                name="ui-static",
            )
        host.install_fallback_index()
        host.start()

    def stop(self) -> None:
        """停止 uvicorn 并清空服务状态（幂等，未启动时可安全调用）。"""
        host = self._host
        try:
            if host is not None:
                host.stop()
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
        """登记插件 UI contribution 和显式配置的静态资源根目录。"""
        if not contribution_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", contribution_id):
            raise ValueError("contribution_id 只能包含字母、数字、点、下划线和连字符")
        accepted_formats = {
            "page": {"vue"},
            "slot": {"vue", "html"},
            "script": {"js"},
        }
        if kind not in accepted_formats or format not in accepted_formats[kind]:
            raise ValueError(f"不支持的 UI 类型与资源格式组合：{kind!r}/{format!r}")
        if kind == "page" and (not path or not title):
            raise ValueError("page contribution 必须提供 path 和 title")
        if kind == "slot" and not target:
            raise ValueError("slot contribution 必须提供 target")
        if not isinstance(cast(object, entry), str) or not entry.strip():
            raise ValueError("entry 必须是 resource_root 下的相对文件路径")

        entry_path = Path(entry.replace("\\", "/"))
        if entry_path.is_absolute() or any(part in {"", ".", ".."} for part in entry_path.parts):
            raise ValueError("entry 必须是 resource_root 下不含路径穿越的相对路径")
        allowed_suffixes: tuple[str, ...]
        if format == "vue":
            allowed_suffixes = (".vue",)
        elif format == "html":
            allowed_suffixes = (".html",)
        else:
            allowed_suffixes = (".js", ".mjs")
        if entry_path.suffix.lower() not in allowed_suffixes:
            raise ValueError(f"{format} contribution 的 entry 扩展名不正确")

        root = Path(resource_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"插件 UI 资源目录不存在：{root}")
        resolved_entry = (root / entry_path).resolve()
        if not resolved_entry.is_relative_to(root) or not resolved_entry.is_file():
            raise ValueError(f"插件 UI 入口文件不存在或超出资源目录：{entry}")

        caller_fiber = self._caller_fiber()
        owner = _fiber_name(caller_fiber)
        owner_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", owner).strip("-") or "plugin"
        full_id = f"{owner}:{contribution_id}"
        if full_id in self._ui:
            raise ValueError(f"UI contribution 已注册：{full_id}")

        static_path = f"/plugin-ui/{owner_slug}/{contribution_id}"
        resource_record = RouteRecord(
            path=static_path,
            kind="static",
            methods=frozenset({"GET"}),
            owner=owner,
            directory=str(root),
        )
        self._registry.add(resource_record)
        item = UiContribution(
            id=full_id,
            kind=kind,
            format=format,
            owner=owner,
            entry=f"{static_path}/{entry_path.as_posix()}",
            path=normalize_path(path) if path else None,
            title=title,
            menu_group=menu_group,
            menu_icon=menu_icon,
            menu_order=menu_order,
            target=target,
            order=order,
        )
        self._ui[full_id] = item
        caller_fiber.effect(
            lambda: lambda: self._remove_ui(full_id),
            f"ctx.web.register_ui({full_id!r})",
        )
        return item

    def ui_contributions(self) -> list[UiContribution]:
        """返回按类型、排序和 ID 稳定排序的前端扩展清单。"""
        return sorted(
            self._ui.values(),
            key=lambda item: (item.kind, item.menu_order, item.order, item.title or item.id),
        )

    def _remove_ui(self, contribution_id: str) -> None:
        item = self._ui.pop(contribution_id, None)
        if item is not None:
            self._registry.remove(f"/plugin-ui/{_owner_slug(item.owner)}/{contribution_id.rsplit(':', 1)[-1]}")

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
        self._ui.clear()
        self._host = None

    def __repr__(self) -> str:
        state = "running" if self.is_running() else "stopped"
        return f"UvicornWebService({state}, {len(self._registry)} routes, {len(self._ui)} ui)"


def _fiber_name(fiber: Fiber | None) -> str:
    """取 fiber 名用于诊断（``RouteRecord.owner``）；无 fiber 时回退 ``"root"``。"""
    return getattr(fiber, "name", None) or "root"


def _owner_slug(owner: str) -> str:
    """把插件名转换为 URL path segment。"""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", owner).strip("-") or "plugin"


__all__: list[str] = ["ServerError", "UvicornHost", "UvicornWebService"]
