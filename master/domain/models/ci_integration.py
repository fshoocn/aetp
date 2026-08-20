"""领域对象：CI/CD 集成与触发绑定（P8.3，§8.8）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ProjectIntegration:
    """项目 CI/CD 集成。"""

    id: int | None = None
    integration_id: str = ""
    project_id: str = ""
    provider: str = ""
    name: str = ""
    secret_hash: str | None = None
    # sym:secret_ref 原始 secret 的密钥引用（加密存 secret_values，§12.2）
    secret_ref: str | None = None
    config_json: dict = field(default_factory=dict)
    enabled: bool = True
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class CiTriggerBinding:
    """CI 触发绑定（集成 ↔ 任务定义）。"""

    id: int | None = None
    binding_id: str = ""
    integration_id: str = ""
    task_id: str = ""
    event_filter_json: dict = field(default_factory=dict)
    parameter_mapping_json: dict = field(default_factory=dict)
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class CiWebhookDelivery:
    """CI Webhook 投递记录（去重和审计）。"""

    id: int | None = None
    integration_id: str = ""
    delivery_id: str = ""
    received_at: datetime | None = None
    payload_hash: str = ""
    status: str = "accepted"
    triggered_run_ids: list[str] = field(default_factory=list)
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
