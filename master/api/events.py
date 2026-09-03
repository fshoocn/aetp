""" 项目领域事件 SSE。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from master.api.dependencies import EventBusDep, UowFactoryDep
from master.api.permissions import ProjectAccessDep
from master.domain.models import DomainEvent as PersistedEvent

router = APIRouter(prefix="/api/v2/events", tags=["events"])
_KEEPALIVE_SECONDS = 15.0


@router.get("")
async def stream_events(
    request: Request,
    project_id: str,
    _access: ProjectAccessDep,
    uow_factory: UowFactoryDep,
    event_bus: EventBusDep,
) -> StreamingResponse:
    raw_last_event_id = request.headers.get("last-event-id", "0")
    try:
        last_sequence = max(0, int(raw_last_event_id or "0"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Last-Event-ID 必须是非负整数序号",
        ) from exc

    async def event_stream():
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
                yield _format_event(_to_event(persisted))
                current_sequence = max(current_sequence, persisted.sequence or 0)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                    if event is None or event.event_type == "server.shutdown":
                        break
                    if event.sequence is not None and event.sequence <= current_sequence:
                        continue
                    yield _format_event(event)
                    current_sequence = max(current_sequence, event.sequence or 0)
                except TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            await event_bus.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _to_event(event: PersistedEvent):
    from master.adapters.sse.event import DomainEvent

    return DomainEvent(
        event_type=event.event_type,
        data=event.payload,
        ts=event.occurred_at.isoformat() if event.occurred_at else "",
        event_id=event.event_id,
        sequence=event.sequence,
        project_id=event.project_id,
    )


def _format_event(event) -> str:
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
    return f"id: {event.sequence or event.event_id!s}\nevent: {event.event_type}\ndata: {payload}\n\n"


__all__ = ["router"]
