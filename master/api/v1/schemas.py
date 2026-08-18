"""v1 项目 API 的请求与响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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
    """Run 详情（含汇总结果与分片）。"""

    shards: list["ShardOut"] = Field(default_factory=list)
    result: dict | None = None


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
