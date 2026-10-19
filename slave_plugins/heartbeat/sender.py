"""心跳发送：标准库 ``urllib`` + ``asyncio.to_thread``（无第三方 HTTP 客户端）。

依赖清单里没有 ``httpx`` / ``aiohttp``，因此用标准库发心跳；阻塞的
HTTP 调用放进线程执行，不卡事件循环。所有网络层异常统一折算成
:class:`HeartbeatSendError`，让监控层只面对一种错误形态。

出站请求按「最小权限」处理：**不走代理**（``ProxyHandler({})``，环境
变量 ``http_proxy`` 等不该把心跳转发给第三方）、**不跟随重定向**（3xx
按错误状态抛出，防止 POST 载荷被 307 / 308 原样改投到别的主机）；
响应体读取有上限 :data:`MAX_RESPONSE_BYTES`，防异常 / 恶意响应撑爆内存。
"""

from __future__ import annotations

import asyncio
import json
from http.client import HTTPException
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import HeartbeatConfig

#: 响应体读取上限（字节）：心跳应答只承载下发配置，远小于此；超限按失败处理
MAX_RESPONSE_BYTES = 256 * 1024


class HeartbeatSendError(RuntimeError):
    """心跳发送失败（主模块不可达 / 超时 / 返回错误状态 / 响应体超限）。"""


class _NoRedirect(HTTPRedirectHandler):
    """禁止重定向：3xx 一律按错误状态抛出，避免 POST 载荷被改投到别的主机。"""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


#: 心跳专用 opener：不读环境代理、不跟随重定向
_OPENER = build_opener(ProxyHandler({}), _NoRedirect())


def parse_response_body(body: bytes) -> dict[str, object]:
    """把响应体解析成 JSON 对象；不合规时返回空字典（视为「无配置」）。

    空 body / 非 JSON / JSON 数组 / 深层嵌套（``json.loads`` 会抛
    ``RecursionError``，注意它不是 ``ValueError`` 子类）都算不合规——
    心跳是否成功只看 HTTP 状态，响应内容只用于主模块下发配置。
    """
    try:
        parsed: object = json.loads(body) if body else None
    except (ValueError, RecursionError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # isinstance 收窄裸 dict 得 dict[Unknown, Unknown]，显式 cast 定型
    return cast(dict[str, object], parsed)


def _post_json_sync(
    endpoint: str, data: bytes, timeout: float, token: str
) -> dict[str, object]:
    """同步 POST（在工作线程里执行），返回解析后的响应对象。

    响应体最多读取 :data:`MAX_RESPONSE_BYTES` 字节；超限抛
    :class:`HeartbeatSendError`（应答只承载下发配置，不该这么大）。
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-AETP-Token"] = token
    request = Request(
        endpoint,
        data=data,
        headers=headers,
        method="POST",
    )
    with _OPENER.open(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise HeartbeatSendError(
            f"主模块响应体过大（超过 {MAX_RESPONSE_BYTES} 字节）"
        )
    return parse_response_body(body)


class HeartbeatSender:
    """按 :class:`HeartbeatConfig` 向主模块发送心跳的发送器。"""

    def __init__(self, config: HeartbeatConfig) -> None:
        self._config = config

    async def send(self, payload: dict[str, object]) -> dict[str, object]:
        """发送一次心跳载荷，返回主模块响应对象；失败抛
        :class:`HeartbeatSendError`。"""
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            # TypeError：载荷含不可序列化对象；ValueError：循环引用。
            # 属于本模块的 bug，但也统一形态，别让裸异常穿透到监控层。
            raise HeartbeatSendError(f"心跳载荷无法序列化：{exc}") from exc
        try:
            return await asyncio.to_thread(
                _post_json_sync,
                self._config.endpoint(),
                data,
                self._config.timeout,
                self._config.auth_token,
            )
        except HTTPError as exc:
            # 主模块在线但接口出错（4xx/5xx）或重定向被拒（3xx）——
            # 属于「被拒绝」而非「不可达」
            raise HeartbeatSendError(f"主模块返回错误状态 {exc.code}") from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                # 连接 / 读取超时会被 urllib 包成 URLError(socket.timeout)
                raise HeartbeatSendError(
                    f"主模块响应超时（超过 {self._config.timeout} 秒）"
                ) from exc
            # 连接被拒、DNS 解析失败等：主模块不在线的典型形态
            raise HeartbeatSendError(f"主模块不可达：{exc.reason}") from exc
        except TimeoutError as exc:
            raise HeartbeatSendError(
                f"主模块响应超时（超过 {self._config.timeout} 秒）"
            ) from exc
        except (OSError, HTTPException, ValueError) as exc:
            # OSError：连接重置等网络层残余；HTTPException：坏响应行 / 不完整读；
            # ValueError：非法 URL
            raise HeartbeatSendError(f"心跳请求失败：{exc}") from exc
