"""通用路由/API handler 返回值转换为 HTTP 响应。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from .types import RouteRecord


def _json_response(value: object) -> Response:
    """构造 JSON 响应；不可序列化时给出可操作的报错。"""
    try:
        return JSONResponse(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"handler 返回值无法序列化为 JSON（{exc}）；"
            "如需返回自定义内容请改用 ctx.web.json(...) / ctx.web.html(...) "
            "等显式响应对象。"
        ) from exc


def to_response(
    result: object,
    record: RouteRecord,
) -> Response:
    """把 handler 的返回值转换为 Starlette 响应对象。

    参数:
        result: handler 的原始返回值。
        record: 该路由的记录（提供 ``kind``）。
    """
    if isinstance(result, Response):
        return result

    kind = record.kind
    if kind == "api":
        if isinstance(result, (bytes, bytearray)):
            return Response(bytes(result))
        if isinstance(result, str):
            return PlainTextResponse(result)
        if result is None:
            return Response(status_code=204)
        return _json_response(result)

    if isinstance(result, str):
        if result.lstrip().startswith("<"):
            return HTMLResponse(result)
        return PlainTextResponse(result)

    if isinstance(result, (bytes, bytearray)):
        return Response(bytes(result))

    if result is None:
        return Response(status_code=204)

    if isinstance(result, (Mapping, Sequence, bool, int, float)):
        json_value: object = cast(object, result)
        return _json_response(json_value)

    # 兜底：其他对象按其字符串形式返回纯文本，避免 500
    return PlainTextResponse(str(result))


__all__: list[str] = ["to_response"]
