"""管理 API 鉴权：``X-AETP-Token`` 共享密钥 + **可替换验证器接缝**。

当前模型（单密钥）：节点启动必须在 ``config.ini`` 的 ``[web]`` 配置
``auth_token``，所有 ``/api`` 请求携带 ``X-AETP-Token`` 头；比对通过后得到
固定身份 ``{"subject": "operator", "role": "admin"}``。

**接缝**（面向后续的用户管理插件）：中间件只依赖 :class:`AuthVerifier`
协议——验证器把"凭证"折算成"身份"。多用户登录 / 权限管理插件上线后，
把 :attr:`AuthContext.verifier` 换成自己的实现（从请求头 / Cookie 取凭据、
查用户库、返回带角色的身份）即可，中间件、业务路由与前端请求头约定都不
用动；``/api/web/ui`` 等清单类接口可按 ``request.state.auth`` 的身份裁剪
输出（"限制页面展示/功能"的挂点）。
"""

from __future__ import annotations

from typing import Final, Protocol, TypedDict, cast

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

#: 请求头名（前端与心跳上报同款）
AUTH_HEADER: Final[str] = "x-aetp-token"


class AuthIdentity(TypedDict):
    """一次请求的调用方身份（字段可随验证器实现扩充，如 roles / user_id）。"""

    subject: str
    role: str


class AuthVerifier(Protocol):
    """凭证 → 身份的验证器协议（鉴权接缝，可由插件替换实现）。"""

    def verify(self, token: str) -> AuthIdentity | None:
        """凭证有效返回身份，无效返回 ``None``。"""
        ...


class SharedSecretVerifier:
    """默认验证器：共享密钥（``hmac.compare_digest`` 恒时比对）。

    通过者一律得到 ``operator/admin`` 身份——单密钥模型下所有持钥者
    都是操作者。令牌值每次校验现读配置对象，``fiber.update`` 换钥即时生效。
    """

    def __init__(self, config: object) -> None:
        self._config = config

    def verify(self, token: str) -> AuthIdentity | None:
        import hmac

        secret = str(getattr(self._config, "auth_token", "") or "")
        if secret and token and hmac.compare_digest(token, secret):
            return {"subject": "operator", "role": "admin"}
        return None


class AuthContext:
    """鉴权接缝的挂载点：持有可替换的 :class:`AuthVerifier`。

    服务以 :attr:`UvicornWebApiService.auth` 暴露本对象；用户管理插件加载后
    ``ctx.webapi.auth.verifier = MyDbVerifier()`` 即完成升级，无需改动中间件。
    """

    def __init__(self, verifier: AuthVerifier) -> None:
        self.verifier: AuthVerifier = verifier


class AuthMiddleware:
    """``X-AETP-Token`` 鉴权中间件（``/api`` 统一校验）。

    白名单：非 ``/api`` 路径（SPA 页面与静态资源——浏览器导航、ES module
    import 都无法携带自定义头）与 ``OPTIONS``（CORS 预检不带自定义头）。
    校验通过的身份写入 ``scope["state"]["auth"]``，即 ``request.state.auth``。
    """

    def __init__(self, app: ASGIApp, auth: AuthContext) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if not path.startswith("/api") or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        token = Headers(scope=scope).get(AUTH_HEADER, "")
        identity = self.auth.verifier.verify(token) if token else None
        if identity is None:
            response = JSONResponse(
                {"error": "未授权：缺少或无效的 X-AETP-Token"}, status_code=401
            )
            await response(scope, receive, send)
            return

        state = cast(dict[str, object], scope.setdefault("state", {}))
        state["auth"] = identity
        await self.app(scope, receive, send)


__all__: list[str] = [
    "AUTH_HEADER",
    "AuthContext",
    "AuthIdentity",
    "AuthMiddleware",
    "AuthVerifier",
    "SharedSecretVerifier",
]
