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


class TriggerType(StrEnum):
    """Run 触发来源（§18.7，统一 RunRequested 用例）。

    手动/API/定时/CI 为外部触发；retry/recovery 为服务端触发，
    必须引用原 Run 与原因，浏览器不能伪造。
    """

    MANUAL_WEB = "manual_web"
    API = "api"
    SCHEDULE = "schedule"
    CI_WEBHOOK = "ci_webhook"
    RETRY = "retry"
    RECOVERY = "recovery"


class RunStatus(StrEnum):
    """Run 总体状态（§6.4）。

    created → dispatched → acked → running → succeeded / failed / cancelled / timed_out / lost
    """

    CREATED = "created"
    DISPATCHED = "dispatched"
    ACKED = "acked"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LOST = "lost"


class ShardStatus(StrEnum):
    """Shard 状态（Run 内部，§5.4）。

    pending → dispatching → running → succeeded / failed / cancelled / timed_out；
    waiting_recovery 表示等待离线恢复策略。
    """

    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    WAITING_RECOVERY = "waiting_recovery"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ShardAttemptStatus(StrEnum):
    """Shard 向某 Node 的一次派发尝试状态（D-20 历史全量保留）。

    created → dispatched → acked → running → succeeded / failed / cancelled / timed_out；
    unknown 表示 Lease/节点事实暂时丢失，lost 表示对账窗口耗尽。
    """

    CREATED = "created"
    DISPATCHED = "dispatched"
    ACKED = "acked"
    RUNNING = "running"
    UNKNOWN = "unknown"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LOST = "lost"


class CaseStatus(StrEnum):
    """case 级执行结果状态（§6.4）。

    pytest 类插件可运行中实时上报；CANoe 类仅在结束后由插件解析报告产出（D-19）。
    """

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class ArtifactKind(StrEnum):
    """结束产物类型（§6.4）。

    report: 测试报告文件（插件 parse_results 的输入）
    log_archive: 结束后的日志归档
    data: 测量/数据文件
    """

    REPORT = "report"
    LOG_ARCHIVE = "log_archive"
    DATA = "data"


class RunLogLevel(StrEnum):
    """Run 级任务日志等级（§9.4，与 aetp_protocol.LogLevel 一致）。"""

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class OutboxStatus(StrEnum):
    """Outbox 消息投递状态（事务性 outbox，§6.2/§8.6）。

    pending: 已随业务事务落库，待 worker 取走
    sending: 已 claim，正在发送
    succeeded: 发送成功（MQTT ACK）
    retrying: 发送失败待重试
    exhausted: 重试耗尽
    cancelled: 取消发送
    """

    PENDING = "pending"
    SENDING = "sending"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


class DeviceStatus(StrEnum):
    """设备运行状态。"""

    OFFLINE = "offline"
    ONLINE = "online"
    BUSY = "busy"


class NodeStatus(StrEnum):
    """节点运行状态（D-22：P4 节点在线投影新增 busy/disabled）。

    offline: 离线（未连接/心跳超时/LWT）
    online: 在线（已注册且会话有效）
    busy: 在线且有活动任务（由调度器在派发时设置，P4.6）
    disabled: 被平台管理员禁用（禁用的节点不可被调度）
    """

    OFFLINE = "offline"
    ONLINE = "online"
    BUSY = "busy"
    DISABLED = "disabled"


class DisconnectReason(StrEnum):
    """节点会话断开原因（§8.6 LWT / 节点会话管理）。

    unexpected_disconnect: 非正常离线（LWT 触发）
    normal_shutdown: 正常关闭
    session_replaced: 同一节点新会话注册，旧会话被替换
    expired: 会话过期（心跳超时）
    """

    UNEXPECTED_DISCONNECT = "unexpected_disconnect"
    NORMAL_SHUTDOWN = "normal_shutdown"
    SESSION_REPLACED = "session_replaced"
    EXPIRED = "expired"


class NotificationChannelType(StrEnum):
    """通知端点通道类型（§10.5）。"""

    EMAIL = "email"
    GENERIC_WEBHOOK = "generic_webhook"
    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    SLACK = "slack"
    TEAMS = "teams"
    CONSOLE_TEST = "console_test"


class DeliveryStatus(StrEnum):
    """投递状态（§10.5，§10.6）。"""

    PENDING = "pending"
    SENDING = "sending"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"
