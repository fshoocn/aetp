"""默认传输实现（HTTP）：批量 ingest / 长轮询 pull / ack。

协议单选（决策 A2）：本实现即默认实现；将来换协议由实现
:class:`~common.plugins.comm.interface.CommTransport` 的新类**整体替换**，
配置不做协议选择。线上协议（``docs/comm通信方案-2026-10-22.md`` §6/§9）：

* ``deliver`` → ``POST /api/comm/ingest``，body ``{"node", "items"}``（gzip）；
* ``pull``    → ``POST /api/comm/poll``，body ``{"node", "hang", "max"}``
  → 应答 ``{"items"}``（长轮询）；
* ``ack``     → ``POST /api/comm/ack``，body ``{"node", "ids"}``。

出站请求不走环境代理（localhost 也可能被代理劫持）、不跟随重定向
（3xx 按错误状态，防 POST 载荷被改投），响应体读取有上限。
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, cast

from .interface import Envelope, TransportResult

#: 单次 POST 的超时（秒）
POST_TIMEOUT: float = 10.0
#: pull 应答体读取上限（防异常响应撑爆内存）
MAX_RESPONSE_BYTES: int = 256 * 1024
#: 单次 pull 拉取的下行条数上限
MAX_PULL_ITEMS: int = 100


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止重定向：3xx 一律按错误状态抛出，避免 POST 载荷被改投。"""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


#: 传输专用 opener：不读环境代理、不跟随重定向
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect()
)


class HttpTransport:
    """``CommTransport`` 的默认 HTTP 实现。"""

    def __init__(
        self, target: str, token: str, node_name: Callable[[], str]
    ) -> None:
        self._base = target.rstrip("/")
        self._token = token
        self._node_name = node_name

    # ------------------------------------------------------------- CommTransport
    async def deliver(self, items: list[Envelope]) -> TransportResult:
        """批量 gzip 上报；按状态码归类错误（协议错误 vs 可重试）。"""
        try:
            status = await asyncio.to_thread(
                self._post, "/api/comm/ingest", {"node": self._node_name(), "items": items}, True
            )
        except urllib.error.HTTPError as exc:
            status = exc.code  # 4xx/5xx 显示真相，不当网络错误
        except (urllib.error.URLError, OSError, ValueError):
            return {"ok": False, "error_kind": "network"}
        if 200 <= status < 300:
            return {"ok": True, "error_kind": ""}
        if 400 <= status < 500 and status != 429:
            return {"ok": False, "error_kind": "protocol"}
        return {"ok": False, "error_kind": "network"}  # 5xx / 429：可重试

    async def pull(self, hang: float) -> list[Envelope]:
        """长轮询拉取下行信封；失败返回空列表（下一轮再拉）。"""
        try:
            data = await asyncio.to_thread(
                self._post_json,
                "/api/comm/poll",
                {"node": self._node_name(), "hang": hang, "max": MAX_PULL_ITEMS},
            )
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
            return []
        raw = data.get("items")
        if not isinstance(raw, list):
            return []
        return [
            cast(Envelope, item)
            for item in cast(list[object], raw)
            if isinstance(item, dict)
        ]

    async def ack(self, ids: list[str]) -> None:
        """确认下行已处理；失败仅忽略（对端靠超时重投，at-least-once 兜底）。"""
        if not ids:
            return
        with contextlib.suppress(
            urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError
        ):
            await asyncio.to_thread(
                self._post_json, "/api/comm/ack", {"node": self._node_name(), "ids": ids}
            )

    async def close(self) -> None:
        """无持久连接，无资源可释放。"""

    # --------------------------------------------------------------- HTTP 原语
    def _post(self, path: str, body: dict[str, object], compress: bool) -> int:
        """同步 POST，返回 HTTP 状态码；网络层异常向上抛。"""
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if compress:
            raw = gzip.compress(raw)
            headers["Content-Encoding"] = "gzip"
        # 节点名只随 body 传递：HTTP 头限 latin-1，中文节点名会炸请求构造
        if self._token:
            headers["X-AETP-Token"] = self._token
        request = urllib.request.Request(
            f"{self._base}{path}", data=raw, headers=headers, method="POST"
        )
        with _OPENER.open(request, timeout=POST_TIMEOUT) as response:
            return int(response.status)

    def _post_json(self, path: str, body: dict[str, object]) -> dict[str, object]:
        """同步 POST 并解析 JSON 应答；超限 / 非 JSON 视为无内容。"""
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-AETP-Token"] = self._token
        request = urllib.request.Request(
            f"{self._base}{path}", data=raw, headers=headers, method="POST"
        )
        with _OPENER.open(request, timeout=POST_TIMEOUT) as response:
            text = response.read(MAX_RESPONSE_BYTES + 1)
        empty: dict[str, object] = {}
        if len(text) > MAX_RESPONSE_BYTES:
            return empty
        try:
            parsed: object = json.loads(text) if text else None
        except (ValueError, RecursionError):
            return empty
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
        return empty


__all__: list[str] = ["MAX_PULL_ITEMS", "MAX_RESPONSE_BYTES", "POST_TIMEOUT", "HttpTransport"]
