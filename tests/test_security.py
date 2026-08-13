"""JWT 安全配置测试。"""

from __future__ import annotations

import jwt
import pytest

import master.config as config
from master.api.v1.security import (
    WeakSecretError,
    create_access_token,
    decode_access_token,
)


def test_jwt_secret_is_revalidated_after_config_reset(tmp_path):
    """配置切换为弱密钥后，不能沿用之前的校验缓存。"""
    valid_env = tmp_path / "valid.env"
    valid_env.write_text(
        "AETP_MASTER_JWT_SECRET=valid-secret-at-least-32-bytes-for-test\n",
        encoding="utf-8",
    )
    config.reset_settings()
    config.configure(valid_env)
    create_access_token("1")

    weak_env = tmp_path / "weak.env"
    weak_env.write_text("AETP_MASTER_JWT_SECRET=weak\n", encoding="utf-8")
    config.reset_settings()
    config.configure(weak_env)

    with pytest.raises(WeakSecretError):
        create_access_token("1")


def test_access_token_contains_issuer_and_audience(tmp_path):
    """P2.9：令牌携带 iss/aud/jti，且能被自身配置解码。"""
    env = tmp_path / "iss_aud.env"
    env.write_text(
        "AETP_MASTER_JWT_SECRET=valid-secret-at-least-32-bytes-for-test\n"
        "AETP_MASTER_JWT_ISSUER=test-issuer\n"
        "AETP_MASTER_JWT_AUDIENCE=test-audience\n",
        encoding="utf-8",
    )
    config.reset_settings()
    config.configure(env)

    token = create_access_token("42")
    payload = decode_access_token(token)
    assert payload["sub"] == "42"
    assert payload["iss"] == "test-issuer"
    assert payload["aud"] == "test-audience"
    assert payload["jti"]


def test_decode_rejects_wrong_issuer(tmp_path):
    """P2.9：签发方不匹配的令牌被拒绝。"""
    env_a = tmp_path / "a.env"
    env_a.write_text(
        "AETP_MASTER_JWT_SECRET=valid-secret-at-least-32-bytes-for-test\n"
        "AETP_MASTER_JWT_ISSUER=issuer-a\n"
        "AETP_MASTER_JWT_AUDIENCE=test-audience\n",
        encoding="utf-8",
    )
    config.reset_settings()
    config.configure(env_a)
    token = create_access_token("42")

    env_b = tmp_path / "b.env"
    env_b.write_text(
        "AETP_MASTER_JWT_SECRET=valid-secret-at-least-32-bytes-for-test\n"
        "AETP_MASTER_JWT_ISSUER=issuer-b\n"
        "AETP_MASTER_JWT_AUDIENCE=test-audience\n",
        encoding="utf-8",
    )
    config.reset_settings()
    config.configure(env_b)

    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token)


def test_decode_rejects_wrong_audience(tmp_path):
    """P2.9：受众不匹配的令牌被拒绝。"""
    env_a = tmp_path / "a.env"
    env_a.write_text(
        "AETP_MASTER_JWT_SECRET=valid-secret-at-least-32-bytes-for-test\n"
        "AETP_MASTER_JWT_ISSUER=test-issuer\n"
        "AETP_MASTER_JWT_AUDIENCE=audience-a\n",
        encoding="utf-8",
    )
    config.reset_settings()
    config.configure(env_a)
    token = create_access_token("42")

    env_b = tmp_path / "b.env"
    env_b.write_text(
        "AETP_MASTER_JWT_SECRET=valid-secret-at-least-32-bytes-for-test\n"
        "AETP_MASTER_JWT_ISSUER=test-issuer\n"
        "AETP_MASTER_JWT_AUDIENCE=audience-b\n",
        encoding="utf-8",
    )
    config.reset_settings()
    config.configure(env_b)

    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token)
