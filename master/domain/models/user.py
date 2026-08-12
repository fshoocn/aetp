"""领域对象：平台用户。

纯 Python 对象，不依赖 ORM/FastAPI；由仓储负责与持久化层转换。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from master.domain.enums import AccountStatus, PlatformRole


@dataclass
class User:
    """平台登录用户。

    id: 内部代理主键（持久化后填充）
    username: 全局唯一用户名
    password_hash: Argon2id 密码哈希
    account_status: pending / active / disabled
    platform_role: user / admin
    """

    id: int | None
    username: str
    password_hash: str
    display_name: str
    account_status: AccountStatus
    platform_role: PlatformRole
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        """是否已激活（active 才允许登录与访问 API）。"""
        return self.account_status == AccountStatus.ACTIVE

    @property
    def is_admin(self) -> bool:
        """是否平台管理员。"""
        return self.platform_role == PlatformRole.ADMIN

    @property
    def persisted_id(self) -> int:
        """已持久化用户的 id。

        id 在新建（未落库）时为 None，但从数据库加载的用户必然已持久化。
        API/权限层拿到的一定是已加载用户，应使用本属性以获得 int 类型。
        """
        if self.id is None:
            raise ValueError("用户尚未持久化，无法获取 id")
        return self.id
