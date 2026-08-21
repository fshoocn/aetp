"""SQLAlchemy 用户仓储实现。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.orm import User as UserORM
from master.domain.enums import AccountStatus, PlatformRole
from master.domain.models import User
from master.domain.repositories import UserRepository


def _to_domain(orm: UserORM) -> User:
    return User(
        id=orm.id,
        username=orm.username,
        password_hash=orm.password_hash,
        display_name=orm.display_name,
        account_status=AccountStatus(orm.account_status),
        platform_role=PlatformRole(orm.platform_role),
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class UserRepositoryImpl(UserRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_id(self, user_id: int) -> User | None:
        orm = self._s.get(UserORM, user_id)
        return _to_domain(orm) if orm is not None else None

    def get_by_username(self, username: str) -> User | None:
        orm = self._s.execute(select(UserORM).where(UserORM.username == username)).scalar_one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list(self, *, account_status: str | None = None, limit: int = 50, offset: int = 0) -> list[User]:
        stmt = select(UserORM).order_by(UserORM.id).limit(limit).offset(offset)
        if account_status:
            stmt = stmt.where(UserORM.account_status == account_status)
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def count(self) -> int:
        return self._s.execute(select(func.count()).select_from(UserORM)).scalar_one()

    def add(self, user: User) -> User:
        orm = UserORM(
            username=user.username,
            password_hash=user.password_hash,
            display_name=user.display_name,
            account_status=user.account_status.value,
            platform_role=user.platform_role.value,
        )
        self._s.add(orm)
        self._s.flush()
        return _to_domain(orm)

    def update(self, user: User) -> User:
        orm = self._s.get(UserORM, user.id)
        if orm is None:
            raise ValueError(f"用户不存在: id={user.id}")
        orm.username = user.username
        orm.password_hash = user.password_hash
        orm.display_name = user.display_name
        orm.account_status = user.account_status.value
        orm.platform_role = user.platform_role.value
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
