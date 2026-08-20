"""领域对象：加密密钥记录（§12.2/§10.5）。

密钥以密文存储（``secret_values`` 表），业务层只持有 ``secret_ref``；
明文仅在 SecretStore.get 解回后短暂存在于内存，绝不落日志/SSE/审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.time import utcnow


@dataclass
class SecretValueRecord:
    """加密密钥记录（secret_values 表）。"""

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:secret_ref 密钥引用（业务层持有；唯一）
    secret_ref: str = ""
    # sym:cipher_text Fernet 密文（urlsafe base64）
    cipher_text: str = ""
    # sym:created_at 创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime = field(default_factory=utcnow)


SecretValueRecord.__test__ = False
