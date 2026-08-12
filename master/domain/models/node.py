"""领域对象：Agent 节点。

运行 Agent 的执行端（电脑），可管理多个外设 Device。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from master.domain.enums import NodeStatus


@dataclass
class Node:
    """运行 Agent 的执行节点。

    tags: 节点标签列表
    capabilities: 节点能力字典（JSON）
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
    capabilities: dict = field(default_factory=dict)
    protocol_version: str = ""
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    devices: list = field(default_factory=list)
