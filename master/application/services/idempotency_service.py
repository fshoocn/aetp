"""写 API 持久化幂等服务。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError

from master.domain.models.idempotency import IdempotencyRecord
from master.domain.time import utcnow


class IdempotencyConflict(ValueError):
    """同一幂等键被不同请求或并发请求占用。"""


@dataclass(frozen=True)
class IdempotencyReservation:
    record: IdempotencyRecord
    is_new: bool


class IdempotencyService:
    def __init__(self, uow_factory, *, ttl_s: int = 86400) -> None:
        if ttl_s < 1:
            raise ValueError("idempotency ttl 必须大于 0")
        self._uow_factory = uow_factory
        self._ttl = timedelta(seconds=ttl_s)

    def reserve(
        self,
        key: str | None,
        scope: str,
        payload: Mapping[str, object],
    ) -> IdempotencyReservation | None:
        if key is None:
            return None
        normalized_key = key.strip()
        if not normalized_key or len(normalized_key) > 255 or any(ord(char) < 32 for char in normalized_key):
            raise ValueError("Idempotency-Key 不合法")
        if not scope or len(scope) > 512:
            raise ValueError("幂等作用域不合法")
        request_hash = _payload_hash(payload)
        now = utcnow()
        with self._uow_factory() as uow:
            existing = uow.idempotency_records.get(scope, normalized_key)
            if existing is not None:
                if existing.expires_at <= now:
                    uow.idempotency_records.delete(scope, normalized_key)
                elif existing.request_hash != request_hash:
                    raise IdempotencyConflict("同一 Idempotency-Key 不能复用不同请求")
                else:
                    return IdempotencyReservation(existing, is_new=False)
            try:
                record = uow.idempotency_records.add(
                    IdempotencyRecord(
                        key=normalized_key,
                        scope=scope,
                        request_hash=request_hash,
                        expires_at=now + self._ttl,
                    )
                )
                return IdempotencyReservation(record, is_new=True)
            except IntegrityError:
                raise IdempotencyConflict("请求正在由其他实例处理") from None

    def complete(
        self,
        reservation: IdempotencyReservation,
        *,
        response_status: int,
        response_body: Mapping[str, object],
    ) -> None:
        record = reservation.record
        if record.id is None:
            raise ValueError("幂等记录尚未持久化")
        with self._uow_factory() as uow:
            record.status = "completed"
            record.response_status = response_status
            record.response_body = dict(response_body)
            uow.idempotency_records.update(record)

    def release(self, reservation: IdempotencyReservation) -> None:
        record = reservation.record
        with self._uow_factory() as uow:
            uow.idempotency_records.delete(record.scope, record.key)


def _payload_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["IdempotencyConflict", "IdempotencyReservation", "IdempotencyService"]
