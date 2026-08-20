"""SQLAlchemy 加密密钥仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import SecretValue as SecretValueORM
from master.domain.models import SecretValueRecord
from master.domain.repositories import SecretValueRepository


def _to_domain(orm: SecretValueORM) -> SecretValueRecord:
    return SecretValueRecord(
        id=orm.id,
        secret_ref=orm.secret_ref,
        cipher_text=orm.cipher_text,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class SecretValueRepositoryImpl(SecretValueRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, secret_ref: str) -> SecretValueRecord | None:
        orm = self._s.execute(
            select(SecretValueORM).where(SecretValueORM.secret_ref == secret_ref)
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def upsert(self, secret_ref: str, cipher_text: str) -> SecretValueRecord:
        orm = self._s.execute(
            select(SecretValueORM).where(SecretValueORM.secret_ref == secret_ref)
        ).scalars().one_or_none()
        if orm is None:
            orm = SecretValueORM(secret_ref=secret_ref, cipher_text=cipher_text)
            self._s.add(orm)
        else:
            orm.cipher_text = cipher_text
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def delete(self, secret_ref: str) -> None:
        orm = self._s.execute(
            select(SecretValueORM).where(SecretValueORM.secret_ref == secret_ref)
        ).scalars().one_or_none()
        if orm is not None:
            self._s.delete(orm)
            self._s.flush()
