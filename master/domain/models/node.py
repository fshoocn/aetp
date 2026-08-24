"""领域对象：Agent 节点与会话。

运行 Agent 的执行端（电脑），可管理多个外设 Device。
NodeSession 记录 Agent 的 MQTT 会话（每次进程启动一个 session_id，
用于隔离旧连接、会话校验与在线投影，§8.6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aetp_protocol.capabilities import NodeCapabilities

from master.domain.enums import DisconnectReason, NodeStatus


@dataclass
class Node:
    """运行 Agent 的执行节点。

    tags: 节点标签列表
    capabilities: 公共强类型 NodeCapabilities（JSON 仅在持久化边界使用）
    load: 节点负载（心跳上报的结构化字段 {running_shards, queued_shards}，§18.5）
    devices: 该节点下的外设列表（查询时由仓储加载）
    """

    id: int | None
    node_id: str
    name: str
    hostname: str
    status: NodeStatus
    online: bool
    enabled: bool
    tags: list = field(default_factory=list)
    capabilities: NodeCapabilities = field(default_factory=NodeCapabilities)
    protocol_version: str = ""
    plugin_versions: dict[str, str] = field(default_factory=dict)
    plugin_supported_versions: dict[str, list[str]] = field(default_factory=dict)
    last_seen_at: datetime | None = None
    load: dict = field(default_factory=dict)
    # sym:resource_occupancy 资源占用映射（device_id -> 占用它的 run_id，§9.8）。
    # 由 Agent 心跳汇总活跃 Run 的 device_allocations 上报，用于资产页展示
    # 「谁占了哪个口」；调度仍以 Device.status=BUSY 为权威。
    resource_occupancy: dict = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    devices: list = field(default_factory=list)


@dataclass
class NodeSession:
    """节点的一次 MQTT 会话（node_sessions 表）。

    (node_pk, session_id) 唯一；每次进程启动生成新的 session_id，
    新会话注册时旧会话被关闭（SESSION_REPLACED），旧 session 的后续
    消息被拒绝（P4.4 验收：旧 session 消息被拒绝）。
    """

    # sym:id 代理主键（自增 int）
    id: int | None = None
    # sym:node_pk 所属节点代理主键（FK nodes.id）
    node_pk: int = 0
    # sym:node_id 节点业务标识（冗余，便于查询展示）
    node_id: str = ""
    # sym:session_id Agent 进程启动生成的会话 ID（envelope.sender.session_id）
    session_id: str = ""
    # sym:client_id MQTT client_id（诊断用）
    client_id: str = ""
    # sym:connected_at 会话建立时间（UTC）
    connected_at: datetime | None = None
    # sym:disconnected_at 会话关闭时间（非空=已关闭）
    disconnected_at: datetime | None = None
    # sym:disconnect_reason 断开原因（DisconnectReason）
    disconnect_reason: DisconnectReason | None = None
    # sym:created_at 记录创建时间（UTC）
    created_at: datetime | None = None
    # sym:updated_at 最后更新时间（UTC）
    updated_at: datetime | None = None
