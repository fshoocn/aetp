"""webapi 拦截链的请求现场与阶段词表（Phase 1 / 设计 §4.2、§4.4）。

依赖 Starlette，因此不放 ``types.py``——后者约定不依赖 starlette。拦截器经
``ctx.webapi.use(...)`` 注册进 :class:`common.hook_chain.HookChain`，阶段由
宿主（webapi）定义；插件拦截器只依赖本模块的 :class:`RequestContext`。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode

from starlette.requests import Request
from starlette.responses import Response

from common.hook_chain import Interceptor, Next, Terminal

from .auth import AuthIdentity
from .types import RouteRecord

#: webapi 的阶段词表（由**宿主**定义；别的宿主有自己的词表）：
#: 最外层 observe → 廉价前置 guard → 授权 authorize → 最靠近 handler 的 inner
WEBAPI_PHASES: tuple[str, ...] = ("observe", "guard", "authorize", "inner")

#: 请求增强（:meth:`RequestContext.enhance`）的 body 上限：拦截链把大 body
#: 缓存进内存会变成攻击面，超限直接拒绝
MAX_ENHANCE_BODY_BYTES: int = 64 * 1024


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

    def enhance(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> None:
        """请求增强：改写 handler 视角的请求（在 ``next()`` 之前调用）。

        * ``headers``：合并进请求头（同名覆盖；ASGI 侧统一小写键）；
        * ``query``：合并进查询串（同名覆盖，重复键折叠为最后一个）——
          路由匹配已完成，只影响 handler 读到的查询参数；
        * ``body``：替换请求体（≤ :data:`MAX_ENHANCE_BODY_BYTES`，超限抛
          ``ValueError``），必须在 handler 首次读 body 之前调用（starlette
          首读即缓存）；写入的是 starlette 的 body 缓存槽——``stream()``
          流式读取**绕过缓存**（增强不生效），升级 starlette 后跑
          ``tests/test_webapi_pipeline`` 的 enhance 用例即兼容哨兵。

        响应改写不需要这里：直接替换 ``await nxt()`` 的返回值即可（对应
        Spring 的 ``postHandle`` / ``ResponseBodyAdvice``）。
        """
        scope = self.request.scope
        if headers:
            overrides = {
                key.lower().encode("latin-1"): value.encode("latin-1")
                for key, value in headers.items()
            }
            current = cast("list[tuple[bytes, bytes]]", scope["headers"])
            current[:] = [(key, value) for key, value in current if key not in overrides]
            current.extend(overrides.items())
        if query:
            merged = dict(
                parse_qsl(
                    cast(bytes, scope.get("query_string", b"")).decode("latin-1"),
                    keep_blank_values=True,
                )
            )
            merged.update({str(key): str(value) for key, value in query.items()})
            scope["query_string"] = urlencode(merged).encode("latin-1")
        if body is not None:
            if len(body) > MAX_ENHANCE_BODY_BYTES:
                raise ValueError(
                    f"请求增强 body 超限：{len(body)} > {MAX_ENHANCE_BODY_BYTES} 字节"
                )
            # starlette 的 body 缓存槽（Request.body() 首读即缓存进 _body），
            # 无公开写入口——经 Any 落槽
            cast("Any", self.request)._body = body  # noqa: SLF001


type WebNext = Next[Response]
type WebInterceptor = Interceptor[RequestContext, Response]
type WebTerminal = Terminal[RequestContext, Response]

__all__: list[str] = [
    "MAX_ENHANCE_BODY_BYTES",
    "WEBAPI_PHASES",
    "RequestContext",
    "WebInterceptor",
    "WebNext",
    "WebTerminal",
]
