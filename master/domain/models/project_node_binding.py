"""领域对象：项目-节点绑定。

限制项目可调度节点范围的绑定记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aetp_protocol.capabilities import NodeCapabilities

from master.domain.models.device import Device


@dataclass
class ProjectNodeBinding:
    """项目与节点的绑定关系。"""

    id: int | None
    project_id: str
    node_id: str
    enabled: bool
    assigned_by: int
    created_at: datetime
    updated_at: datetime


@dataclass
class ProjectNodeBindingView:
    """节点绑定及其节点信息的查询结果视图。"""

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
    capabilities: NodeCapabilities = field(default_factory=NodeCapabilities)
    plugin_versions: dict[str, str] = field(default_factory=dict)
    devices: list[Device] = field(default_factory=list)
