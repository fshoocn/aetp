"""加密密钥存储适配器（SecretStore 端口实现，§10.5/§12.2）。

密钥以 Fernet 对称加密后落库（``secret_values`` 表），业务层只持有
``secret_ref``；日志/SSE/审计永不回显明文。

加密密钥派生自 ``AETP_MASTER_JWT_SECRET``（确定性派生），因此：
- 只要 JWT secret 不变，Master 重启后仍能解密既有密钥；
- 数据库泄露（无 JWT secret）不泄露通知/CI 明文密钥。
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable

from cryptography.fernet import Fernet, InvalidToken

from common.secret_derivation import derive_key
from master.domain.notifications import SecretStore, SecretValue
from master.domain.repositories import UnitOfWork

logger = logging.getLogger(__name__)


def derive_fernet_key(secret: str) -> bytes:
    """由主密钥经 HKDF 按 ``secret-store`` 用途派生 32 字节 Fernet 密钥。

    Fernet 要求 32 字节密钥的 urlsafe base64 编码；与 JWT 签名、内部签名
    密钥隔离（分用途派生），同一主密钥重启后得到同一加密密钥。
    """
    digest = derive_key(secret, "secret-store", length=32)
    return base64.urlsafe_b64encode(digest)


class EncryptedSecretStore(SecretStore):
    """Fernet 加密的持久化密钥存储。

    密钥值写入 ``secret_values`` 表（密文）；``get`` 解回明文供
    HMAC 签名类 sender / webhook 验证使用。依赖 UnitOfWork 端口，不接触
    具体数据库实现。
    """

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        master_secret: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._fernet = Fernet(derive_fernet_key(master_secret))

    def set(self, secret_ref: str, value: str) -> None:
        """加密并持久化一个密钥值（幂等覆盖）。"""
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        with self._uow_factory() as uow:
            uow.secret_values.upsert(secret_ref, token)
        logger.debug("密钥已加密存储: secret_ref=%s", secret_ref)

    def get(self, secret_ref: str) -> SecretValue | None:
        """按引用解回密钥明文；不存在或解密失败返回 None。"""
        with self._uow_factory() as uow:
            record = uow.secret_values.get(secret_ref)
        if record is None:
            return None
        try:
            plain = self._fernet.decrypt(record.cipher_text.encode("ascii"))
        except (InvalidToken, ValueError):
            logger.error("密钥解密失败（主密钥可能已变更）: secret_ref=%s", secret_ref)
            return None
        return SecretValue(value=plain.decode("utf-8"))

    def delete(self, secret_ref: str) -> None:
        """删除密钥（端点/集成删除时调用）。"""
        with self._uow_factory() as uow:
            uow.secret_values.delete(secret_ref)
        logger.debug("密钥已删除: secret_ref=%s", secret_ref)
