"""SecretStore 持久化（Fernet 加密）测试（§12.2/§10.5）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from master.adapters.secrets.encrypted_store import (
    EncryptedSecretStore,
    derive_fernet_key,
)
from master.domain.models import SecretValueRecord


def _mock_uow():
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _make_store(uow, master_secret: str = "test-secret-32-bytes-minimum-length") -> EncryptedSecretStore:
    return EncryptedSecretStore(
        uow_factory=lambda: uow,
        master_secret=master_secret,
    )


def test_derive_fernet_key_is_deterministic():
    """同一主密钥派生同一 Fernet 密钥；不同主密钥不同。"""
    k1 = derive_fernet_key("secret-a")
    k2 = derive_fernet_key("secret-a")
    k3 = derive_fernet_key("secret-b")
    assert k1 == k2
    assert k1 != k3


def test_set_encrypts_and_get_decrypts():
    """set 落库的是密文（非明文），get 解回明文。"""
    uow = _mock_uow()
    store = _make_store(uow)

    store.set("ref-1", "plain-secret-value")

    # set 写入的是 Fernet 密文（upsert 的第二个参数）
    written_ref, written_cipher = uow.secret_values.upsert.call_args[0]
    assert written_ref == "ref-1"
    assert written_cipher != "plain-secret-value"
    assert "plain-secret-value" not in written_cipher

    # get 从仓储读到密文后解回明文
    uow.secret_values.get.return_value = SecretValueRecord(
        id=1, secret_ref="ref-1", cipher_text=written_cipher
    )
    result = store.get("ref-1")
    assert result is not None
    assert result.value == "plain-secret-value"


def test_get_missing_returns_none():
    """密钥不存在返回 None。"""
    uow = _mock_uow()
    uow.secret_values.get.return_value = None
    store = _make_store(uow)
    assert store.get("missing") is None


def test_get_decrypt_failure_returns_none():
    """密文损坏或主密钥变更时解密失败返回 None（不抛异常）。"""
    uow = _mock_uow()
    uow.secret_values.get.return_value = SecretValueRecord(
        id=1, secret_ref="ref-1", cipher_text="not-a-valid-fernet-token"
    )
    store = _make_store(uow)
    assert store.get("ref-1") is None


def test_delete_removes_secret():
    """delete 调用仓储删除。"""
    uow = _mock_uow()
    store = _make_store(uow)
    store.delete("ref-1")
    uow.secret_values.delete.assert_called_once_with("ref-1")


def test_cross_store_decryption_fails_with_different_master_secret():
    """不同主密钥派生的 store 无法解密对方密文（数据库泄露 + 无主密钥无法解回）。"""
    uow_a = _mock_uow()
    store_a = _make_store(uow_a, master_secret="master-secret-A")
    store_a.set("ref-1", "top-secret")
    cipher = uow_a.secret_values.upsert.call_args[0][1]

    uow_b = _mock_uow()
    uow_b.secret_values.get.return_value = SecretValueRecord(
        id=1, secret_ref="ref-1", cipher_text=cipher
    )
    store_b = _make_store(uow_b, master_secret="master-secret-B")
    assert store_b.get("ref-1") is None
