"""v1 API JWT 令牌签发与校验工具。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from master.config import get_settings

ALGORITHM = "HS256"
_MIN_KEY_BYTES = 32
_DEV_SECRET = "aetp-master-dev-secret-change-me"
_last_checked_secret: str | None = None


class WeakSecretError(RuntimeError):
    """JWT 密钥过于薄弱或使用了开发默认值。"""


def _assert_secret_strong(secret: str) -> None:
    if secret == _DEV_SECRET:
        raise WeakSecretError(
            "JWT 密钥仍为开发默认值，请在 .env 中设置 AETP_JWT_SECRET 为至少 "
            f"{_MIN_KEY_BYTES} 字节的随机值后重启"
        )
    if len(secret.encode("utf-8")) < _MIN_KEY_BYTES:
        raise WeakSecretError(
            f"JWT 密钥长度不足（当前 {len(secret.encode('utf-8'))} 字节，至少需要 {_MIN_KEY_BYTES} 字节）"
        )


def _ensure_secret_checked() -> None:
    global _last_checked_secret
    secret = get_settings().jwt_secret
    if secret == _last_checked_secret:
        return
    _assert_secret_strong(secret)
    _last_checked_secret = secret


def validate_security_settings() -> None:
    """启动时校验 JWT 密钥强度；弱密钥直接抛错拒绝启动。

    在组合根（run.py / main.py lifespan）调用，避免服务
    在弱配置下"看似正常"、直到第一个登录请求才失败。
    """
    _ensure_secret_checked()


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    """签发访问令牌（subject 为用户 id 字符串）。"""
    _ensure_secret_checked()
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """校验并解码令牌；无效或过期抛出 jwt.PyJWTError。"""
    _ensure_secret_checked()
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
