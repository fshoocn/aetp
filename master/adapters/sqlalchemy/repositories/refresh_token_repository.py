"""SQLAlchemy 刷新令牌仓储实现。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import RefreshToken as RefreshTokenORM
from master.domain.models import RefreshToken
from master.domain.repositories import RefreshTokenRepository
from master.domain.time import utcnow


def _to_domain(orm: RefreshTokenORM) -> RefreshToken:
    return RefreshToken(
        id=orm.id,
        user_id=orm.user_pk,
        token_hash=orm.token_hash,
        expires_at=orm.expires_at,
        revoked_at=orm.revoked_at,
        replaced_by_hash=orm.replaced_by_hash,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class RefreshTokenRepositoryImpl(RefreshTokenRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        orm = self._s.execute(
            select(RefreshTokenORM).where(RefreshTokenORM.token_hash == token_hash)
        ).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def add(self, token: RefreshToken) -> RefreshToken:
        orm = RefreshTokenORM(
            user_pk=token.user_id,
            token_hash=token.token_hash,
            expires_at=token.expires_at,
            revoked_at=token.revoked_at,
            replaced_by_hash=token.replaced_by_hash,
        )
        self._s.add(orm)
        self._s.flush()
        return _to_domain(orm)

    def update(self, token: RefreshToken) -> RefreshToken:
        orm = self._s.get(RefreshTokenORM, token.id)
        if orm is None:
            raise ValueError(f"刷新令牌不存在: id={token.id}")
        orm.revoked_at = token.revoked_at
        orm.replaced_by_hash = token.replaced_by_hash
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def revoke_all_for_user(self, user_id: int) -> int:
        """撤销用户全部未撤销的刷新令牌，返回受影响行数。"""
        # Session.execute 类型声明为 Result，但 UPDATE 语句实际返回 CursorResult；
        # rowcount 仅在 CursorResult 上可用，运行时安全，此处用 cast 对齐类型。
        result = cast(
            CursorResult[Any],
            self._s.execute(
                update(RefreshTokenORM)
                .where(
                    RefreshTokenORM.user_pk == user_id,
                    RefreshTokenORM.revoked_at.is_(None),
                )
                .values(revoked_at=utcnow())
            ),
        )
        return result.rowcount or 0
