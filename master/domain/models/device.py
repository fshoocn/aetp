"""领域对象：设备。

一台物理测试台架上运行的 Agent 外设。device_id 同时用作
MQTT client_id 和 topic 节点标识，平台范围内唯一。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from master.domain.enums import DeviceStatus


@dataclass
class Device:
    """测试台架设备（Agent 的 Master 侧投影）。

    node_id: 所属节点的业务 ID（由仓储通过 node_pk 关联解析）
    """

    id: int | None
    device_id: str
    node_id: str | None
    name: str
    status: DeviceStatus
    online: bool
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
