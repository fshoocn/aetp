"""JWT 安全配置测试。"""

from __future__ import annotations

import pytest

import master.config as config
from master.api.v1.security import WeakSecretError, create_access_token


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