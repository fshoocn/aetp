"""写 API 幂等键领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.time import utcnow


@dataclass
class IdempotencyRecord:
    id: int | None = None
    key: str = ""
    scope: str = ""
    request_hash: str = ""
    status: str = "pending"
    response_status: int | None = None
    response_body: dict | None = None
    expires_at: datetime = field(default_factory=utcnow)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
