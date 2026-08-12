"""认证业务服务。

职责：
- 注册用户（Argon2id 密码哈希入库，初始 pending 零权限）
- 登录凭据校验（Argon2id 验证）
- 修改密码
- 平台管理员审批用户（P2.4）

通过 UnitOfWork 访问领域仓储，不直接接触数据库会话。
"""

from __future__ import annotations

import logging
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from master.application.errors import (
    InvalidCredentialsError,
    UsernameAlreadyExistsError,
)
from master.domain.enums import AccountStatus, PlatformRole
from master.domain.models import User
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

# 全局单例 PasswordHasher（线程安全），使用 Argon2id 变体
# time_cost=3, memory_cost=65536KB, parallelism=4：适合服务器/工控机场景
_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32)
logger = logging.getLogger(__name__)


def _hash_password(password: str) -> str:
    """Argon2id 哈希密码，返回编码字符串（含 salt 和参数）。"""
    return _ph.hash(password)


def _verify_password(password: str, stored_hash: str) -> bool:
    """校验 Argon2id 密码；不匹配返回 False。"""
    try:
        return _ph.verify(stored_hash, password)
    except VerificationError:
        return False


class AuthService:
    """用户认证服务。"""

    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ---- 登录 ----

    def authenticate(self, username: str, password: str) -> User | None:
        """校验账号+密码是否匹配（Argon2id）。

        返回 User 对象（认证成功），否则返回 None。
        账号不存在 / 密码错误均返回 None（统一口径，防账号探测）。
        """
        with self._uow_factory() as uow:
            user = uow.users.get_by_username(username)
            if user is None:
                logger.warning("认证失败：用户不存在: username=%s", username)
                return None
            if not _verify_password(password, user.password_hash):
                logger.warning("认证失败：密码错误: username=%s", username)
                return None
            logger.info(
                "用户认证成功: user_id=%s, username=%s", user.id, username
            )
            return user

    def authenticate_or_raise(self, username: str, password: str) -> User:
        """校验失败时抛出 InvalidCredentialsError（API 层直接转 401）。"""
        user = self.authenticate(username, password)
        if user is None:
            raise InvalidCredentialsError("用户名或密码错误")
        return user

    # ---- 注册 / 修改密码 ----

    def create_user(
        self,
        username: str,
        password: str,
        display_name: str = "",
    ) -> User:
        """创建用户（Argon2id 哈希）。

        新用户默认 account_status="pending"、platform_role="user"，
        注册请求中不得传入角色字段。用户名已存在时抛异常。
        """
        with self._uow_factory() as uow:
            if uow.users.get_by_username(username) is not None:
                raise UsernameAlreadyExistsError(f"用户名已存在: {username}")
            user = User(
                id=None,
                username=username,
                password_hash=_hash_password(password),
                display_name=display_name,
                account_status=AccountStatus.PENDING,
                platform_role=PlatformRole.USER,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            created = uow.users.add(user)
            logger.info(
                "用户注册成功: user_id=%s, username=%s, status=pending",
                created.id,
                username,
            )
            return created

    def change_password(self, username: str, new_password: str) -> bool:
        """修改密码（使用 Argon2id 哈希）；用户不存在返回 False。"""
        with self._uow_factory() as uow:
            user = uow.users.get_by_username(username)
            if user is None:
                return False
            user.password_hash = _hash_password(new_password)
            user.updated_at = utcnow()
            uow.users.update(user)
            logger.info("用户密码已修改: username=%s", username)
            return True

    def bootstrap_admin(
        self,
        username: str,
        password: str,
        display_name: str = "Platform Admin",
    ) -> bool:
        """在用户表为空时创建首个平台管理员。

        返回 True 表示创建成功，False 表示已有用户而跳过。
        并发启动时的唯一键冲突交由调用方处理，避免服务因重复 bootstrap 失败。
        """
        with self._uow_factory() as uow:
            if uow.users.count() > 0:
                return False
            user = User(
                id=None,
                username=username,
                password_hash=_hash_password(password),
                display_name=display_name,
                account_status=AccountStatus.ACTIVE,
                platform_role=PlatformRole.ADMIN,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            uow.users.add(user)
            logger.info(
                "bootstrap 管理员创建成功: user_id=%s, username=%s",
                user.id,
                username,
            )
            return True

    # ---- 平台管理员审批（2.4） ----

    def list_users(
        self,
        account_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[User]:
        """分页查询用户列表；管理员按 account_status 筛选 pending 待审批账户。"""
        with self._uow_factory() as uow:
            users = uow.users.list(
                account_status=account_status, limit=limit, offset=offset
            )
            logger.debug(
                "查询用户列表: account_status=%s, count=%s",
                account_status,
                len(users),
            )
            return users

    def approve_user(
        self,
        user_id: int,
        *,
        account_status: str | None = None,
        platform_role: str | None = None,
    ) -> User | None:
        """平台管理员审批/编辑账户属性。

        仅传入的字段会被更新，未传入的字段保持原值。
        用户不存在返回 None。
        """
        with self._uow_factory() as uow:
            user = uow.users.get_by_id(user_id)
            if user is None:
                return None
            if account_status is not None:
                user.account_status = AccountStatus(account_status)
            if platform_role is not None:
                user.platform_role = PlatformRole(platform_role)
            user.updated_at = utcnow()
            updated = uow.users.update(user)
            logger.info(
                "用户权限已更新: user_id=%s, account_status=%s, platform_role=%s",
                user_id,
                updated.account_status,
                updated.platform_role,
            )
            return updated
