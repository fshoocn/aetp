"""SSE 实时事件流路由。

浏览器通过 fetch + ReadableStream 订阅（可携带 Authorization 头），
收到的事件为 `data: <json>` 行，格式：
    {"type": "task.created", "data": {...}, "ts": "..."}

事件类型：
    task.created     任务创建
    task.updated     任务状态变更（后续执行器接入后）
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from master.api.v1.dependencies import CurrentUser, EventBusDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["v1-events"])

# 心跳间隔：客户端断线探测与代理超时规避
_KEEPALIVE_SECONDS = 15.0


@router.get("")
async def stream_events(
    request: Request,
    _current_user: CurrentUser,
    event_bus: EventBusDep,
) -> StreamingResponse:
    """订阅实时事件流（SSE）。"""

    async def event_stream():
        queue = await event_bus.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                    if event.type == "server.shutdown":
                        # 生命周期关闭前由 Master 主动通知 SSE 客户端结束流，
                        # 避免 Uvicorn 只能通过取消任务强行切断长连接。
                        break
                    payload = json.dumps(
                        {
                            "type": event.type,
                            "data": event.data,
                            "ts": event.ts,
                        },
                        ensure_ascii=False,
                    )
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            # Uvicorn 优雅关闭超时后会取消 SSE 生成器；必须立即退订，
            # 不能等待下一次 keepalive 或继续持有连接。
            raise
        finally:
            await event_bus.unsubscribe(queue)
            logger.debug("SSE 连接关闭")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
