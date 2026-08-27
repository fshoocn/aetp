"""确定性密钥派生（HKDF-SHA256，分用途隔离）。

AETP 有三类基于主密钥（``AETP_MASTER_JWT_SECRET``）的派生密钥：

- ``jwt-signing``：访问令牌 HMAC 签名（jwt.encode/decode）；
- ``internal-signing``：内部下载端点限时 HMAC 签名（脚本/插件/产物下载）；
- ``secret-store``：``EncryptedSecretStore`` 的 Fernet 加密密钥。

三者必须隔离：任一用途的派生密钥泄露不得导致其他用途密钥泄露。
用 HKDF-SHA256 从同一主密钥按 ``purpose``（作为 info）确定性派生，
保证主密钥不变时重启后仍得到同一派生密钥（与旧实现确定性一致），
同时不同用途得到不同密钥材料。
"""

from __future__ import annotations

import hashlib
import hmac

_HASH = hashlib.sha256


def derive_key(master_secret: str, purpose: str, *, length: int = 32) -> bytes:
    """用 HKDF-SHA256 从主密钥按用途派生 ``length`` 字节密钥。

    实现遵循 RFC 5869 的 extract-then-expand：salt 固定为空串的 hash
    （等价于无 salt），info 为 ``b"aetp:" + purpose``。
    """
    ikm = master_secret.encode("utf-8")
    info = f"aetp:{purpose}".encode("utf-8")
    # extract：PRK = HMAC(salt, IKM)，salt 为空串
    prk = hmac.new(b"", ikm, _HASH).digest()
    # expand：T(1) = HMAC(PRK, T(0) || info || 0x01)，单块即可覆盖 32 字节
    t = hmac.new(prk, info + b"\x01", _HASH).digest()
    return t[:length]


def derive_hex(master_secret: str, purpose: str, *, length: int = 32) -> str:
    """派生密钥并返回十六进制字符串（供需要字符串密钥的 HMAC 用途）。"""
    return derive_key(master_secret, purpose, length=length).hex()
