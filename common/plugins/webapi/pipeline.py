"""webapi 拦截链的请求现场与阶段词表（Phase 1 / 设计 §4.2、§4.4）。

依赖 Starlette，因此不放 ``types.py``——后者约定不依赖 starlette。拦截器经
``ctx.webapi.use(...)`` 注册进 :class:`common.hook_chain.HookChain`，阶段由
宿主（webapi）定义；插件拦截器只依赖本模块的 :class:`RequestContext`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import Response

from common.hook_chain import Interceptor, Next, Terminal

from .auth import AuthIdentity
from .types import RouteRecord

#: webapi 的阶段词表（由**宿主**定义；别的宿主有自己的词表）：
#: 最外层 observe → 廉价前置 guard → 授权 authorize → 最靠近 handler 的 inner
WEBAPI_PHASES: tuple[str, ...] = ("observe", "guard", "authorize", "inner")


@dataclass(slots=True)
class RequestContext:
    """一次请求穿过拦截链的现场（拦截器间经 ``state`` 传数据）。"""

    request: Request
    route: RouteRecord
    #: 身份（即 ``request.state.auth``；令牌 / 同源校验在链外层的 ASGI 中间件）
    identity: AuthIdentity | None
    #: 拦截器之间传递数据的字典（``next()`` 不带参数，数据只经这里流动）
    state: dict[str, object] = field(default_factory=dict[str, object])
    #: 进入链时的单调钟时刻（``time.perf_counter()``）
    started: float = 0.0


type WebNext = Next[Response]
type WebInterceptor = Interceptor[RequestContext, Response]
type WebTerminal = Terminal[RequestContext, Response]

__all__: list[str] = [
    "WEBAPI_PHASES",
    "RequestContext",
    "WebInterceptor",
    "WebNext",
    "WebTerminal",
]
