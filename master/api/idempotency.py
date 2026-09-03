""" 写 API 的幂等键辅助函数。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from fastapi import HTTPException, status
from pydantic import BaseModel

from master.application.services.idempotency_service import (
    IdempotencyConflict,
    IdempotencyReservation,
    IdempotencyService,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass(frozen=True)
class IdempotencyResult(Generic[ResponseT]):
    reservation: IdempotencyReservation | None
    response: ResponseT | None
    replayed: bool


def reserve_or_replay(
    service: IdempotencyService,
    key: str | None,
    *,
    scope: str,
    payload: Mapping[str, object],
    response_model: type[ResponseT] | None,
) -> IdempotencyResult[ResponseT]:
    try:
        reservation = service.reserve(key, scope, payload)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if reservation is None:
        return IdempotencyResult(None, None, False)
    if reservation.is_new:
        return IdempotencyResult(reservation, None, False)
    if reservation.record.status != "completed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="相同请求正在处理中")
    if response_model is None:
        return IdempotencyResult(None, None, True)
    if reservation.record.response_body is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="幂等响应已损坏")
    return IdempotencyResult(
        None,
        response_model.model_validate(reservation.record.response_body),
        True,
    )


def complete(
    service: IdempotencyService,
    reservation: IdempotencyReservation | None,
    response: BaseModel | Mapping[str, Any],
    *,
    response_status: int,
) -> None:
    if reservation is None:
        return
    body = response.model_dump(mode="json") if isinstance(response, BaseModel) else dict(response)
    service.complete(reservation, response_status=response_status, response_body=body)


def release(service: IdempotencyService, reservation: IdempotencyReservation | None) -> None:
    if reservation is not None:
        service.release(reservation)


__all__ = ["IdempotencyResult", "complete", "release", "reserve_or_replay"]
