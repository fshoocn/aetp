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

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from master.api.v1.dependencies import EventBusDep, UowFactoryDep
from master.api.v1.permissions import ProjectAccessDep
from master.adapters.sse.event import DomainEvent
from master.domain.models import DomainEvent as PersistedEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["v1-events"])

# 心跳间隔：客户端断线探测与代理超时规避
_KEEPALIVE_SECONDS = 15.0


@router.get("")
async def stream_events(
    request: Request,
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    event_bus: EventBusDep,
) -> StreamingResponse:
    """订阅项目范围 SSE，并从 Last-Event-ID 之后回放历史事件。"""
    raw_last_event_id = request.headers.get("last-event-id", "0")
    try:
        last_sequence = max(0, int(raw_last_event_id or "0"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Last-Event-ID 必须是非负整数序号",
        ) from exc

    async def event_stream():
        # 先订阅再查询历史，避免回放期间漏掉新事件。
        queue = await event_bus.subscribe(project_id)
        try:
            yield ": connected\n\n"
            with uow_factory() as uow:
                replay = uow.domain_events.list(
                    project_id=project_id,
                    after_sequence=last_sequence,
                    limit=1000,
                )
            current_sequence = last_sequence
            for persisted in replay:
                yield _format_event(_to_sse(persisted))
                current_sequence = max(current_sequence, persisted.sequence or 0)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                    if event.event_type == "server.shutdown":
                        # 生命周期关闭前由 Master 主动通知 SSE 客户端结束流，
                        # 避免 Uvicorn 只能通过取消任务强行切断长连接。
                        break
                    if event.sequence is not None and event.sequence <= current_sequence:
                        continue
                    yield _format_event(event)
                    current_sequence = max(current_sequence, event.sequence or 0)
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


def _to_sse(event: PersistedEvent) -> DomainEvent:
    """持久化领域事件 → SSE 推送载荷。"""
    return DomainEvent(
        event_type=event.event_type,
        data=event.payload,
        ts=event.occurred_at.isoformat() if event.occurred_at else "",
        event_id=event.event_id,
        sequence=event.sequence,
        project_id=event.project_id,
    )


def _format_event(event: DomainEvent) -> str:
    """编码标准 SSE event/id/data 字段。"""
    payload = json.dumps(
        {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "project_id": event.project_id,
            "type": event.event_type,
            "data": event.data,
            "ts": event.ts,
        },
        ensure_ascii=False,
    )
    return f"id: {str(event.sequence or event.event_id)}\nevent: {event.event_type}\ndata: {payload}\n\n"
