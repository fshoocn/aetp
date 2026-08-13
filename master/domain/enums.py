"""领域层枚举：账户状态、平台角色、项目角色、任务/设备/节点状态。

所有枚举值使用小写 snake_case 字符串，方便序列化。
ORM 与 API DTO 统一从本模块取值，杜绝魔法字符串。
"""

from __future__ import annotations

from enum import StrEnum


class AccountStatus(StrEnum):
    """平台用户账户状态。

    pending: 已注册、等待管理员审批，不能获取访问令牌和业务权限
    active: 管理员已激活，可被加入项目
    disabled: 管理员禁用，不可登录
    """

    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class PlatformRole(StrEnum):
    """平台全局角色。

    user: 普通用户，权限受项目成员关系限制
    admin: 平台管理员，可跨项目操作、审核账户、管理项目和节点
    """

    USER = "user"
    ADMIN = "admin"


class ProjectStatus(StrEnum):
    """项目生命周期状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectRole(StrEnum):
    """项目内角色，仅在所属项目范围内生效。

    viewer: 只读任务、设备、结果、日志
    operator: viewer 权限 + 触发、取消、重试 Run
    maintainer: operator 权限 + 编辑任务定义、管理项目集成和普通成员
    owner: 项目完整管理权限
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    MAINTAINER = "maintainer"
    OWNER = "owner"


class TaskStatus(StrEnum):
    """测试任务状态机（D-22 目标命名，P3.1 已迁移）。

    pending → dispatching → running → succeeded / failed / timed_out
    pending → cancelled
    dispatching → failed（派发耗尽）
    running → cancelling → cancelled

    旧值迁移映射：dispatched/accepted → dispatching；
    completed → succeeded；timeout → timed_out。
    """

    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ScriptParseStatus(StrEnum):
    """测试脚本用例解析状态。"""

    PENDING = "pending"
    PARSING = "parsing"
    PARSED = "parsed"
    FAILED = "failed"


class ScriptParseLocation(StrEnum):
    """用例解析/结果解析执行位置（D-17）。"""

    MASTER = "master"
    AGENT = "agent"


class SplitPolicyType(StrEnum):
    """任务分割策略类型（§18.6，D-19/D-21）。

    none: 不分割，单 Shard
    by_time: 按 case 平均耗时切分（依赖 avg_duration_s，D-21）
    by_case_count: 按用例数量切分
    custom: 插件自定义分割
    """

    NONE = "none"
    BY_TIME = "by_time"
    BY_CASE_COUNT = "by_case_count"
    CUSTOM = "custom"


class DeviceStatus(StrEnum):
    """设备运行状态。"""

    OFFLINE = "offline"
    ONLINE = "online"
    BUSY = "busy"


class NodeStatus(StrEnum):
    """节点运行状态。"""

    OFFLINE = "offline"
    ONLINE = "online"
