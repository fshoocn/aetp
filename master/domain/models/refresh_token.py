"""领域对象：刷新令牌会话。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RefreshToken:
    id: int | None
    user_id: int
    token_hash: str
    expires_at: datetime
    revoked_at: datetime | None
    replaced_by_hash: str | None
    created_at: datetime
    updated_at: datetime
