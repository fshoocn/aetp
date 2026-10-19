"""节点通信与接口鉴权：``X-AETP-Token`` 机器凭证 + 同源受信 + **可叠加的验证器接缝**。

两层模型（**并存，不是二选一**）：

* **机器层（本模块）**：``X-AETP-Token`` 是**节点通信令牌**——主从通信
  （心跳上报、未来的插件分发 / 数据回传）与脚本化 API 调用的凭证；从节点
  不考虑用户登录，机器通道全靠它。浏览器**同源**请求自动受信
  （``Sec-Fetch-Site: same-origin/none``，页面已挂在受信后端上）。
* **用户层（未来的用户管理插件）**：管"人"——登录态、可见内容裁剪、页面
  展示效果、接口访问权限。它在本模块之上**叠加**（包装验证器 / 附加权限
  中间件），不取代令牌机制；同源受信给出的只是匿名操作者身份，插件可
  收紧为"未登录则拒绝"。

**接缝**：中间件只依赖 :class:`AuthVerifier` 协议——"请求 → 身份"。机器
身份（令牌比对）与用户身份（登录态）可由同一个验证器组合解析；用户管理
插件加载后 ``ctx.webapi.auth.verifier = UserAwareVerifier(inner=...)``
包装 / 替换即可，中间件与业务路由不动；``/api/web/ui`` 等清单类接口可按
``request.state.auth`` 的身份裁剪输出（"限制页面展示/功能"的挂点）。
"""

from __future__ import annotations

from typing import Final, Protocol, TypedDict, cast

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

#: 请求头名（前端与心跳上报同款）
AUTH_HEADER: Final[str] = "x-aetp-token"


class AuthIdentity(TypedDict):
    """一次请求的调用方身份（可随验证器实现扩充，如 user_id / roles）。

    机器通道（令牌）与用户登录态给出的身份可以在同一验证器里组合解析。
    """

    subject: str
    role: str


class AuthVerifier(Protocol):
    """请求 → 身份的验证器协议（鉴权接缝，可替换 / 可包装以叠加用户层）。"""

    def verify(self, token: str, headers: Headers) -> AuthIdentity | None:
        """按令牌与请求头解析身份；无法认定身份返回 ``None``（拒绝）。"""
        ...


class SharedSecretVerifier:
    """默认验证器：节点令牌（``hmac.compare_digest`` 恒时比对）+ 同源受信。

    * **机器身份**：``X-AETP-Token`` 与配置令牌一致者（主从通信 / 脚本调用）
      得到 ``operator/admin`` 身份；令牌值每次校验现读配置对象，
      ``fiber.update`` 换钥即时生效；
    * **同源受信**：``Sec-Fetch-Site: same-origin/none`` 的浏览器请求
      （页面已挂在受信后端上）按匿名操作者放行——用户层的登录与权限由
      用户管理插件**叠加**（包装本验证器即可收紧），两者不是二选一。
    """

    def __init__(self, config: object) -> None:
        self._config = config

    def verify(self, token: str, headers: Headers) -> AuthIdentity | None:
        import hmac

        secret = str(getattr(self._config, "auth_token", "") or "")
        if secret and token and hmac.compare_digest(token, secret):
            return {"subject": "operator", "role": "admin"}
        if headers.get("sec-fetch-site") in {"same-origin", "none"}:
            return {"subject": "operator", "role": "admin"}
        return None


class AuthContext:
    """鉴权接缝的挂载点：持有可替换 / 可包装的 :class:`AuthVerifier`。

    用户管理插件（登录态 / 权限）可 ``ctx.webapi.auth.verifier =
    UserAwareVerifier(inner=ctx.webapi.auth.verifier)`` **包装叠加**，也可
    整体替换；中间件与业务路由不动。
    """

    def __init__(self, verifier: AuthVerifier) -> None:
        self.verifier: AuthVerifier = verifier


class AuthMiddleware:
    """``X-AETP-Token`` 鉴权中间件（``/api`` 统一校验）。

    白名单：非 ``/api`` 路径（SPA 页面与静态资源——浏览器导航、ES module
    import 都无法携带自定义头）与 ``OPTIONS``（CORS 预检不带自定义头）。
    其余请求交验证器认定身份（默认验证器：令牌比对或同源浏览器放行），
    通过后身份写入 ``scope["state"]["auth"]``，即 ``request.state.auth``。
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

        headers = Headers(scope=scope)
        identity = self.auth.verifier.verify(headers.get(AUTH_HEADER, ""), headers)
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
