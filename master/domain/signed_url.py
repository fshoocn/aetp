"""内部下载端点签名 URL 的纯函数（P4.7，§7.4/§18.8）。

脚本下载不走用户 JWT，而是签发**限时 HMAC 签名 URL**：签名覆盖
``script_id`` 与过期时间 ``expires``，防止任意客户端猜测 ID 批量下载
脚本包。纯函数不依赖配置与框架，便于单元测试与复用。

- ``build_signed_path``：生成相对路径 + ``?expires=...&signature=...``
- ``verify_signed_path``：校验过期与签名（恒定时间比较）
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

_URL_PATH = "/api/v1/internal/scripts/{script_id}/download"


def _signature(script_id: str, expires: int, secret: str) -> str:
    message = f"{script_id}:{expires}".encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()


def build_signed_path(
    script_id: str,
    secret: str,
    ttl_s: int,
    now: datetime | None = None,
) -> str:
    """生成签名下载相对路径（含 query）。"""
    current = now or datetime.now(timezone.utc)
    expires = int(current.timestamp()) + int(ttl_s)
    signature = _signature(script_id, expires, secret)
    path = _URL_PATH.format(script_id=script_id)
    return f"{path}?expires={expires}&signature={signature}"


def verify_signed_path(
    script_id: str,
    expires: int,
    signature: str,
    secret: str,
    now: datetime | None = None,
) -> bool:
    """校验签名 URL：未过期且签名匹配（恒定时间比较，防时序攻击）。"""
    current = now or datetime.now(timezone.utc)
    if expires <= int(current.timestamp()):
        return False
    expected = _signature(script_id, expires, secret)
    return hmac.compare_digest(signature, expected)
