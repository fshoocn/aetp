"""领域对象：刷新令牌会话。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RefreshToken:
    # sym:id 持久化后回填的代理主键
    id: int | None
    # sym:user_id 所属用户代理主键
    user_id: int
    # sym:token_hash 刷新令牌 SHA-256 哈希（不存原始令牌）
    token_hash: str
    # sym:expires_at 过期时间，过期后拒绝刷新
    expires_at: datetime
    # sym:revoked_at 撤销时间（登出/改密/禁用账户），非空即失效
    revoked_at: datetime | None
    # sym:replaced_by_hash 轮换链：被哪个新令牌哈希替代（旧令牌重放检测）
    replaced_by_hash: str | None
    # sym:created_at 签发时间（UTC）
    created_at: datetime
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime
