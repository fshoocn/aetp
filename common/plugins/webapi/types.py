"""webapi 插件的数据模型（路由记录等）。

本模块只有纯数据结构，不依赖 cordis 也不依赖 starlette，便于被任何一方引用 ——
尤其是 :class:`~common.plugins.webapi.interface.WebApiService` 这类**接口**：
接口返回的数据类型不应把具体框架（Starlette）或运行时（cordis）拖进来，
否则「替换底层实现」时连带要把接口一起改掉。
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

#: 路由种类。决定「handler 返回值如何转成 HTTP 响应」以及「如何挂进框架」：
#:
#: * ``api``：JSON 接口，任何结果都按 JSON 序列化；
#: * ``route``：通用路由，按返回值类型智能判断；
#: * ``static``：静态目录挂载（不是 handler，而是静态应用）。
RouteKind = Literal["api", "route", "static"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
StreamContent = Iterable[str | bytes | memoryview] | AsyncIterable[str | bytes | memoryview]
RouteHandler = Callable[..., object | Awaitable[object]]
RouteRegistration = RouteHandler | Callable[[RouteHandler], RouteHandler]
HTTP_METHODS: tuple[HttpMethod, ...] = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
)


@dataclass
class RouteRecord:
    """一条已注册的路由记录。

    属性:
        path: 路由路径，支持 ``/items/{item_id}`` 形式的路径参数。
        kind: 路由种类，见 :data:`RouteKind`。
        methods: 允许的 HTTP 方法集合。
        handler: 用户提供的处理函数；``static`` 为 ``None``。
        endpoint: 包装后的 ASGI 端点，框架实际调用的对象。
        name: 路由名，供 :meth:`~plugins.web.interface.WebService.url_for` 反向生成 URL。
        owner: 注册方插件的 fiber 名，用于诊断。
        directory: 静态目录（``static`` 专用）。
        html: 静态目录是否开启 HTML 模式（``static`` 专用）。
        params: 需要从路径参数自动注入 handler 形参的名字集合。
        converters: 路由参数名 -> 「字符串 → 注解类型」的转换函数。
        route: 框架侧的路由对象缓存（避免重复编译路径）。
    """

    path: str
    kind: RouteKind
    methods: frozenset[HttpMethod] = frozenset({"GET"})
    handler: RouteHandler | None = None
    endpoint: Callable[..., object] | None = None
    name: str | None = None
    owner: str = "root"
    directory: str | None = None
    html: bool = False
    params: frozenset[str] = frozenset()
    converters: dict[str, Callable[[str], int | float | bool | Path]] | None = None
    route: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.kind), str) or self.kind not in get_args(RouteKind):
            raise ValueError(f"不支持的路由种类：{self.kind!r}")
        if (
            not isinstance(cast(object, self.methods), frozenset)
            or not self.methods
            or any(method not in HTTP_METHODS for method in self.methods)
        ):
            raise ValueError(f"HTTP methods 必须是非空的受支持方法集合：{self.methods!r}")

    @property
    def is_static(self) -> bool:
        """是否为静态目录挂载。"""
        return self.kind == "static"


__all__: list[str] = [
    "HttpMethod",
    "RouteHandler",
    "RouteKind",
    "RouteRecord",
    "RouteRegistration",
    "StreamContent",
]
