"""v1 项目 API 的请求与响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aetp_protocol.capabilities import NodeCapabilities

from master.domain.enums import (
    AccountStatus,
    PlatformRole,
    ProjectRole,
    ProjectStatus,
)


class RegisterRequest(BaseModel):
    """注册请求。"""

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str
    password: str


class TokenResponse(BaseModel):
    """登录/刷新响应：短期访问令牌 + 长期刷新令牌。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="访问令牌有效期（秒）")


class RefreshRequest(BaseModel):
    """刷新令牌请求。"""

    refresh_token: str = Field(min_length=16, max_length=512)


class LogoutRequest(BaseModel):
    """登出请求：携带刷新令牌以服务端撤销。"""

    refresh_token: str | None = None


class UserOut(BaseModel):
    """用户响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    account_status: str
    platform_role: str
    created_at: datetime


class DeviceOut(BaseModel):
    """设备响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    node_id: str | None = None
    name: str
    status: str
    online: bool
    last_seen_at: datetime | None = None


class TaskCreate(BaseModel):
    """任务创建请求。"""

    device_id: str
    command: dict = Field(default_factory=dict)


class TaskOut(BaseModel):
    """任务响应。command/result 为结构化 JSON 对象。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str | None = None
    task_id: str
    device_id: str
    status: str
    command: dict = Field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    created_by: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskLogOut(BaseModel):
    """任务日志响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: str
    sequence: int
    level: str
    message: str
    ts: datetime


class RunTriggerRequest(BaseModel):
    """触发一次 Run（P6.4）。"""

    task_id: str = Field(min_length=1, max_length=64)
    case_filter: list[str] | None = None


class RunOut(BaseModel):
    """Run 执行摘要响应。"""

    run_id: str
    project_id: str
    task_id: str
    status: str
    trigger_type: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunDetailOut(RunOut):
    """Run 详情（含汇总结果、分片与 case×attempt 结果矩阵，P7.5）。"""

    shards: list["ShardOut"] = Field(default_factory=list)
    result: dict | None = None
    case_results: list["RunCaseResultOut"] = Field(default_factory=list)


class RunCaseResultOut(BaseModel):
    """case 级执行结果（D-20：按 attempt 全量保留，历史失败可见）。"""

    run_id: str
    shard_id: str
    case_key: str
    attempt_no: int
    status: str
    duration_ms: int | None = None
    error_summary: str | None = None
    detail: dict | None = None


class ShardOut(BaseModel):
    """Run 内 Shard 摘要。"""

    shard_id: str
    shard_index: int
    case_keys: list[str]
    status: str
    final_node: str | None = None


class RunLogOut(BaseModel):
    """Run 执行日志行。"""

    id: int
    run_id: str
    node_id: str
    sequence: int
    level: str
    message: str
    detail: dict | None = None
    occurred_at: datetime | None = None


class RunArtifactOut(BaseModel):
    """Run 结束产物响应。"""

    model_config = ConfigDict(from_attributes=True)

    artifact_id: str
    run_id: str
    shard_id: str | None = None
    node_id: str | None = None
    kind: str
    file_ref: str
    size: int
    sha256: str
    uploaded_at: datetime | None = None


class ProjectCreateRequest(BaseModel):
    """创建项目请求。"""

    project_key: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    owner_id: int | None = Field(default=None, ge=1)


class ProjectUpdateRequest(BaseModel):
    """修改项目元数据请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    status: ProjectStatus | None = None


class ProjectMemberCreateRequest(BaseModel):
    """添加项目成员请求。"""

    user_id: int = Field(ge=1)
    project_role: ProjectRole


class ProjectMemberUpdateRequest(BaseModel):
    """修改项目成员角色请求。"""

    project_role: ProjectRole


class ProjectMemberOut(BaseModel):
    """项目成员响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    user_id: int
    username: str
    display_name: str
    project_role: ProjectRole
    assigned_by: int
    created_at: datetime
    updated_at: datetime


class ProjectNodeBindingCreateRequest(BaseModel):
    """绑定项目节点请求。"""

    node_id: str = Field(min_length=1, max_length=64)


class ProjectNodeBindingUpdateRequest(BaseModel):
    """启用或禁用项目节点绑定请求。"""

    enabled: bool


class NodeDeviceOut(BaseModel):
    """节点下的外设摘要。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    node_id: str | None
    name: str
    status: str
    online: bool


class NodeOut(BaseModel):
    """平台 Node 及其 Device 列表。capabilities 为公共强类型模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: str
    name: str
    hostname: str
    status: str
    online: bool
    enabled: bool
    tags: list = Field(default_factory=list)
    capabilities: NodeCapabilities = Field(default_factory=NodeCapabilities)
    plugin_versions: dict[str, str] = Field(default_factory=dict)
    load: dict = Field(default_factory=dict)
    protocol_version: str
    last_seen_at: datetime | None
    devices: list[NodeDeviceOut]


class ProjectNodeBindingOut(BaseModel):
    """项目节点绑定响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    node_id: str
    name: str
    hostname: str
    status: str
    online: bool
    node_enabled: bool
    enabled: bool
    assigned_by: int
    created_at: datetime
    updated_at: datetime
    capabilities: NodeCapabilities = Field(default_factory=NodeCapabilities)
    plugin_versions: dict[str, str] = Field(default_factory=dict)
    devices: list[NodeDeviceOut]


class ProjectOut(BaseModel):
    """项目响应。"""

    model_config = ConfigDict(from_attributes=True)

    project_id: str
    project_key: str
    name: str
    description: str
    status: ProjectStatus
    created_by: int
    created_at: datetime
    updated_at: datetime
    project_role: ProjectRole | None = None


class AdminUserOut(BaseModel):
    """管理员视角的用户信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    account_status: AccountStatus
    platform_role: PlatformRole
    created_at: datetime


class UserApprovalRequest(BaseModel):
    """管理员审批/编辑用户属性。"""

    account_status: AccountStatus | None = None
    platform_role: PlatformRole | None = None


# ---------------------------------------------------------------------------
# P7.3 脚本库（test_scripts / script_cases）
# ---------------------------------------------------------------------------


class ScriptUploadRequest(BaseModel):
    """脚本上传请求（multipart 表单字段，文件本体由 UploadFile 携带）。"""

    task_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    config: str = Field(default="{}", max_length=65536)


class ScriptOut(BaseModel):
    """脚本版本响应。"""

    model_config = ConfigDict(from_attributes=True)

    script_id: str
    project_id: str
    task_type: str
    name: str
    version: int
    file_ref: str
    size: int
    sha256: str
    parse_status: str
    parse_location: str
    result_parse_location: str
    plugin_version: str
    created_by: int | None = None
    last_parsed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ScriptCaseOut(BaseModel):
    """脚本用例索引项响应。"""

    model_config = ConfigDict(from_attributes=True)

    case_id: str
    script_id: str
    stable_key: str
    name: str
    parent_path: str = ""
    tags: list[str] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)
    avg_duration_s: float | None = None
    duration_samples: int = 0
    order_index: int = 0
    deleted: bool = False


class ScriptVerifyRequest(BaseModel):
    """请求 Agent 在指定项目节点执行插件验证。"""

    node_id: str = Field(min_length=1, max_length=64)
    config: dict = Field(default_factory=dict)


class ScriptVerifyDispatchOut(BaseModel):
    """验证下发回执。"""

    verify_id: str
    project_id: str
    script_id: str
    node_id: str
    status: str


class ScriptVerifyResultOut(BaseModel):
    """Agent 验证结果。"""

    verify_id: str
    script_id: str
    node_id: str
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# P7.4 任务定义（test_tasks）CRUD
# ---------------------------------------------------------------------------


class TestTaskCreateRequest(BaseModel):
    """创建任务定义请求（引用脚本版本 + 默认勾选用例 + 节点/分割/重试策略）。"""

    name: str = Field(min_length=1, max_length=128)
    script_id: str = Field(min_length=1, max_length=64)
    default_case_selection: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    split_policy: dict = Field(default_factory=dict)
    retry_policy: dict = Field(default_factory=dict)
    timeout_s: int = Field(default=0, ge=0)
    priority: int = Field(default=0, ge=0)


class TestTaskUpdateRequest(BaseModel):
    """更新任务定义请求（全量字段，缺失则保持原值）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    script_id: str | None = Field(default=None, min_length=1, max_length=64)
    default_case_selection: list[str] | None = None
    node_ids: list[str] | None = None
    split_policy: dict | None = None
    retry_policy: dict | None = None
    timeout_s: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0)


class TestTaskOut(BaseModel):
    """任务定义响应。"""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    project_id: str
    script_id: str
    script_version: int
    task_type: str
    name: str
    default_case_selection: list[str] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    split_policy: dict = Field(default_factory=dict)
    retry_policy: dict = Field(default_factory=dict)
    timeout_s: int = 0
    enabled: bool = True
    priority: int = 0
    validation_warning: str | None = None
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# P7.6 通知端点 / 事件订阅 / 投递状态
# ---------------------------------------------------------------------------


class EndpointCreate(BaseModel):
    """创建通知端点请求。"""

    channel_type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    config: dict = Field(default_factory=dict)
    secret_value: str | None = Field(default=None, max_length=2048)


class EndpointUpdate(BaseModel):
    """更新通知端点请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    config: dict | None = None
    secret_value: str | None = Field(default=None, max_length=2048)
    enabled: bool | None = None


class EndpointOut(BaseModel):
    """通知端点响应（不含密钥）。"""

    model_config = ConfigDict(from_attributes=True)

    endpoint_id: str
    project_id: str
    channel_type: str
    name: str
    config: dict = Field(default_factory=dict)
    has_secret: bool = False
    enabled: bool = True
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _compute_has_secret(cls, data: object) -> object:
        if hasattr(data, "secret_ref"):
            object.__setattr__(data, "has_secret", bool(data.secret_ref))  # type: ignore[attr-defined]
        elif isinstance(data, dict) and "has_secret" not in data:
            data["has_secret"] = bool(data.get("secret_ref"))
        return data


class SubscriptionCreate(BaseModel):
    """创建事件订阅请求。"""

    endpoint_id: str = Field(min_length=1, max_length=64)
    event_types: list[str] = Field(min_length=1)
    filter_json: dict = Field(default_factory=dict)
    throttle_policy: dict = Field(default_factory=dict)


class SubscriptionUpdate(BaseModel):
    """更新事件订阅请求。"""

    event_types: list[str] | None = None
    filter_json: dict | None = None
    throttle_policy: dict | None = None
    enabled: bool | None = None


class SubscriptionOut(BaseModel):
    """事件订阅响应。"""

    model_config = ConfigDict(from_attributes=True)

    subscription_id: str
    project_id: str
    endpoint_id: str
    event_types: list[str] = Field(default_factory=list)
    filter_json: dict = Field(default_factory=dict)
    throttle_policy: dict = Field(default_factory=dict)
    enabled: bool = True
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeliveryOut(BaseModel):
    """投递记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    delivery_id: str
    project_id: str
    event_id: str
    subscription_id: str
    endpoint_id: str
    status: str
    attempts: int = 0
    next_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    response_summary: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# P8.2 任务调度计划
# ---------------------------------------------------------------------------


class ScheduleCreateRequest(BaseModel):
    """创建调度计划请求（cron 与 interval 互斥）。"""

    cron_expression: str | None = Field(default=None, max_length=128)
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str = Field(default="UTC", max_length=64)
    enabled: bool = True


class ScheduleUpdateRequest(BaseModel):
    """更新调度计划请求。"""

    cron_expression: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None


class ScheduleOut(BaseModel):
    """调度计划响应。"""

    model_config = ConfigDict(from_attributes=True)

    schedule_id: str
    task_id: str
    cron_expression: str | None = None
    interval_seconds: int | None = None
    timezone: str = "UTC"
    enabled: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# P8.3 CI/CD 集成
# ---------------------------------------------------------------------------


class IntegrationCreateRequest(BaseModel):
    """创建 CI/CD 集成请求。"""

    provider: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    secret_value: str | None = Field(default=None, max_length=2048)
    config_json: dict = Field(default_factory=dict)
    enabled: bool = True


class IntegrationUpdateRequest(BaseModel):
    """更新 CI/CD 集成请求。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    secret_value: str | None = Field(default=None, max_length=2048)
    config_json: dict | None = None
    enabled: bool | None = None


class IntegrationOut(BaseModel):
    """CI/CD 集成响应（不含密钥）。"""

    model_config = ConfigDict(from_attributes=True)

    integration_id: str
    project_id: str
    provider: str
    name: str
    has_secret: bool = False
    config_json: dict = Field(default_factory=dict)
    enabled: bool = True
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _compute_has_secret(cls, data: object) -> object:
        if hasattr(data, "secret_hash"):
            object.__setattr__(data, "has_secret", bool(data.secret_hash))  # type: ignore[attr-defined]
        elif isinstance(data, dict) and "has_secret" not in data:
            data["has_secret"] = bool(data.get("secret_hash"))
        return data


class BindingCreateRequest(BaseModel):
    """创建 CI 触发绑定请求。"""

    task_id: str = Field(min_length=1, max_length=64)
    event_filter_json: dict = Field(default_factory=dict)
    parameter_mapping_json: dict = Field(default_factory=dict)


class BindingUpdateRequest(BaseModel):
    """更新 CI 触发绑定请求。"""

    event_filter_json: dict | None = None
    parameter_mapping_json: dict | None = None
    enabled: bool | None = None


class BindingOut(BaseModel):
    """CI 触发绑定响应。"""

    model_config = ConfigDict(from_attributes=True)

    binding_id: str
    integration_id: str
    task_id: str
    event_filter_json: dict = Field(default_factory=dict)
    parameter_mapping_json: dict = Field(default_factory=dict)
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# P8.4 Hook 执行审计
# ---------------------------------------------------------------------------


class HookExecutionOut(BaseModel):
    """Hook 执行审计响应。"""

    model_config = ConfigDict(from_attributes=True)

    execution_id: str
    event_id: str | None = None
    project_id: str | None = None
    hook_name: str
    stage: str
    status: str
    duration_ms: float | None = None
    error_message: str | None = None
    occurred_at: datetime | None = None
    created_at: datetime | None = None
