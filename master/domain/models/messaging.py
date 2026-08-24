"""领域对象：可靠消息与审计（P3.5）。

- InboxMessage：接收端去重的入站消息（(origin_id, message_id) 唯一幂等）。
- OutboxMessage：事务性 outbox——业务状态与待发送消息同一事务提交，
  由后台 worker 可靠发布（§5.1/§8.6）。
- DomainEvent：不可变领域事件（业务事务只持久化聚合状态 + 不可变事件，
  sequence 唯一保证全局顺序，§5.1）。
- AuditLog：审计日志（账户审批、成员变更、角色变更、CI 密钥变更等必须写入，
  §7.6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.enums import OutboxStatus
from master.domain.time import utcnow


@dataclass
class InboxMessage:
    """入站消息去重记录（inbox_messages 表）。

    (origin_id, message_id) 唯一：MQTT 重复投递只处理一次（§5.4 规则 2）。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:origin_id 消息来源标识（如 MQTT client_id / node_id / 集成名）
    origin_id: str = ""
    # sym:message_id 消息源侧唯一 ID（(origin_id, message_id) 唯一=幂等去重键）
    message_id: str = ""
    # sym:message_type 消息类型（命令/结果/心跳等）
    message_type: str = ""
    # sym:payload_hash 载荷哈希（用于内容校验与重复检测）
    payload_hash: str = ""
    # sym:received_at 接收时间（UTC）
    received_at: datetime = field(default_factory=utcnow)
    # sym:processed_at 处理完成时间（非空=已处理，可作处理进度查询）
    processed_at: datetime | None = None
    # sym:created_at 记录创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class OutboxMessage:
    """待可靠发送的 outbox 消息（outbox_messages 表）。

    与业务状态同一事务写入；worker 按 (status, next_attempt_at) 取到期消息，
    发送成功后标记 succeeded，失败按重试策略推进 attempts / next_attempt_at。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:outbox_id Outbox 消息业务标识（ULID），全局唯一
    outbox_id: str = ""
    # sym:aggregate_type 所属聚合类型（如 task_run / node）
    aggregate_type: str = ""
    # sym:aggregate_id 所属聚合业务标识
    aggregate_id: str = ""
    # sym:topic 目标 MQTT 主题（派发/命令下发）
    topic: str = ""
    # sym:payload 消息载荷 JSON（命令/事件内容）
    payload: dict = field(default_factory=dict)
    # sym:qos MQTT QoS（0/1/2）
    qos: int = 1
    # sym:status 投递状态（pending/sending/succeeded/retrying/exhausted/cancelled）
    status: OutboxStatus = OutboxStatus.PENDING
    # sym:attempts 已尝试发送次数（重试计数）
    attempts: int = 0
    # sym:next_attempt_at 下次发送时间（失败退避用；(status, next_attempt_at) 索引）
    next_attempt_at: datetime | None = None
    # sym:sent_at 最近一次发送时间
    sent_at: datetime | None = None
    # sym:created_at 创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class DomainEvent:
    """不可变领域事件（domain_events 表）。

    业务事务提交时一并写入；sequence 全局单调唯一，保证事件顺序
    （SSE 推送 / Hook / 通知按此顺序消费）。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:event_id 事件业务标识（ULID），全局唯一
    event_id: str = ""
    # sym:sequence 全局单调序号（唯一，事件顺序依据；add 时分配）
    sequence: int | None = None
    # sym:project_id 所属项目业务标识（平台级事件为空）
    project_id: str | None = None
    # sym:event_type 事件类型（如 run.created / run.attempt_failed，§10 事件清单）
    event_type: str = ""
    # sym:aggregate_id 关联聚合业务标识（run_id / task_id / node_id 等）
    aggregate_id: str = ""
    # sym:payload 事件载荷 JSON（不可变快照）
    payload: dict = field(default_factory=dict)
    # sym:occurred_at 业务发生时间（UTC）
    occurred_at: datetime = field(default_factory=utcnow)
    # sym:created_at 记录创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class AuditLog:
    """审计日志（audit_logs 表，§7.6）。

    账户审批、成员增删、项目角色变更、CI 集成密钥变更等敏感操作必须写入。
    """

    # sym:id 持久化后回填的代理主键
    id: int | None = None
    # sym:audit_id 审计业务标识（ULID），全局唯一
    audit_id: str = ""
    # sym:project_id 所属项目业务标识（平台级操作可为空）
    project_id: str | None = None
    # sym:actor_id 操作者用户代理主键（系统操作可为空）
    actor_id: int | None = None
    # sym:action 动作名（如 member.add / role.change / integration.key_rotate）
    action: str = ""
    # sym:resource_type 被操作资源类型（project/member/task/...）
    resource_type: str = ""
    # sym:resource_id 被操作资源业务标识
    resource_id: str = ""
    # sym:request_id 关联 HTTP 请求追踪 ID（可空）
    request_id: str | None = None
    # sym:detail 审计详情 JSON（变更前后值等）
    detail: dict = field(default_factory=dict)
    # sym:occurred_at 操作发生时间（UTC）
    occurred_at: datetime = field(default_factory=utcnow)
    # sym:created_at 记录创建时间（UTC）
    created_at: datetime = field(default_factory=utcnow)


# 防止 pytest 将消息域类误识别为测试类（测试文件中会导入本模块）。
InboxMessage.__test__ = False  # type: ignore[reportAttributeAccessIssue]
OutboxMessage.__test__ = False  # type: ignore[reportAttributeAccessIssue]
DomainEvent.__test__ = False  # type: ignore[reportAttributeAccessIssue]
AuditLog.__test__ = False  # type: ignore[reportAttributeAccessIssue]
