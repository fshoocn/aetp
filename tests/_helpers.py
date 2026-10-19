"""测试共用辅助：空闲端口探测与最小 HTTP 请求封装（urllib，与生产同栈）。"""

from __future__ import annotations

import asyncio
import socket
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

#: 无代理 opener：与生产传输层同口径（环境代理可能劫持本机回环请求）
_OPENER = build_opener(ProxyHandler({}))

#: 本进程已发放过的端口：连续探测可能拿到刚还回去的同一端口（TOCTOU），
#: 记账排除，避免同进程两次起服务撞车
_used_ports: set[int] = set()


def free_port() -> int:
    """探测一个当前空闲的本地端口（测试各起真实服务，避免端口撞车）。"""
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        if port not in _used_ports:
            _used_ports.add(port)
            return port


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    """同步发一个请求，返回 ``(状态码, 正文)``；4xx/5xx 不抛错、照常返回。"""
    headers = {"Content-Type": content_type} if content_type else {}
    req = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        response = _OPENER.open(req, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        status = response.status
        payload = response.read()
    if not isinstance(status, int) or not isinstance(payload, bytes):
        raise TypeError("HTTP 响应的状态码或正文类型无效")
    return status, payload


async def arequest(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    """异步包装 :func:`request`（不卡事件循环）。"""
    return await asyncio.to_thread(
        request,
        base_url,
        path,
        method=method,
        body=body,
        content_type=content_type,
    )
