"""API JWT 令牌签发与校验工具。

访问令牌（Access Token）：短期 JWT，携带 iss/aud/jti，解码时严格校验
签发方与受众（P2.9），防止令牌跨系统复用。

刷新令牌（Refresh Token）：不透明随机串（secrets），仅哈希后入库；
刷新时轮换、登出/改密/禁用账户时撤销（P2.10）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from common.secret_derivation import derive_hex
from master.config import get_settings

ALGORITHM = "HS256"
_MIN_KEY_BYTES = 32
_DEV_SECRET = "aetp-master-dev-secret-change-me"
_REFRESH_TOKEN_BYTES = 48
_last_checked_secret: str | None = None


class WeakSecretError(RuntimeError):
    """JWT 密钥过于薄弱或使用了开发默认值。"""


def _signing_key() -> str:
    """访问令牌 HMAC 签名密钥（由主密钥按 ``jwt-signing`` 用途派生）。"""
    return derive_hex(get_settings().jwt_secret, "jwt-signing")


def _assert_secret_strong(secret: str) -> None:
    if secret == _DEV_SECRET:
        raise WeakSecretError(
            "JWT 密钥仍为开发默认值，请在 .env 中设置 AETP_MASTER_JWT_SECRET 为至少 "
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
    """签发访问令牌（subject 为用户 id 字符串）。

    携带 iss/aud（P2.9）与 jti（每次签发唯一，便于审计与将来黑名单）。
    """
    _ensure_secret_checked()
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _signing_key(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """校验并解码令牌；无效/过期/iss/aud 不匹配抛出 jwt.PyJWTError。"""
    _ensure_secret_checked()
    settings = get_settings()
    return jwt.decode(
        token,
        _signing_key(),
        algorithms=[ALGORITHM],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )


def generate_refresh_token() -> str:
    """生成不透明刷新令牌（384-bit 随机数，urlsafe base64）。"""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_refresh_token(raw_token: str) -> str:
    """刷新令牌只以 SHA-256 哈希入库，数据库泄露不泄露原始令牌。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
