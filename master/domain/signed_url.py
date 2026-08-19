"""内部下载端点签名 URL 的纯函数（P4.7，§7.4/§18.8）。

下载不走用户 JWT，而是签发**限时 HMAC 签名 URL**：签名覆盖资源标识
与过期时间 ``expires``，防止任意客户端猜测 ID 批量下载。纯函数不依赖
配置与框架，便于单元测试与复用。

- ``build_signed_path``：生成相对路径 + ``?expires=...&signature=...``
- ``verify_signed_path``：校验过期与签名（恒定时间比较）

脚本下载路径：``/api/v1/internal/scripts/{script_id}/download``
插件下载路径：``/api/v1/internal/plugins/{plugin_id}/download``
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

_SCRIPT_URL_PATH = "/api/v1/internal/scripts/{script_id}/download"
_PLUGIN_URL_PATH = "/api/v1/internal/plugins/{plugin_id}/download"


def _signature(resource_id: str, expires: int, secret: str) -> str:
    message = f"{resource_id}:{expires}".encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()


def build_signed_path(
    resource_id: str,
    secret: str,
    ttl_s: int,
    *,
    path_template: str = _SCRIPT_URL_PATH,
    now: datetime | None = None,
) -> str:
    """生成签名下载相对路径（含 query）。

    :param path_template: 路径模板，含 ``{script_id}``/``{plugin_id}`` 占位；
        默认脚本路径，插件下载传 :data:`_PLUGIN_URL_PATH`。
    """
    current = now or datetime.now(timezone.utc)
    expires = int(current.timestamp()) + int(ttl_s)
    signature = _signature(resource_id, expires, secret)
    path = path_template.format(script_id=resource_id, plugin_id=resource_id)
    return f"{path}?expires={expires}&signature={signature}"


def verify_signed_path(
    resource_id: str,
    expires: int,
    signature: str,
    secret: str,
    now: datetime | None = None,
) -> bool:
    """校验签名 URL：未过期且签名匹配（恒定时间比较，防时序攻击）。"""
    current = now or datetime.now(timezone.utc)
    if expires <= int(current.timestamp()):
        return False
    expected = _signature(resource_id, expires, secret)
    return hmac.compare_digest(signature, expected)
