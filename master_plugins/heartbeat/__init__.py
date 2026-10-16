"""主模块心跳插件（``master_plugins/heartbeat``，id ``heartbeat-server``）。

接收从模块（:mod:`slave_plugins.heartbeat`）周期性上报，维护在线状态与能力存档，
并在主模块 webui 提供从模块管理页。模块划分：

* :mod:`.config` —— ``[heartbeat]`` 配置节（下发参数与判定阈值）；
* :mod:`.registry` —— ``NodeRegistry``：心跳 upsert、状态扫描、状态历史；
* :mod:`.plugin` —— cordis 入口：API 路由 + 状态扫描任务 + 管理页注册。

协议契约（载荷 / 应答 / 能力信封）见 ``master_plugins/heartbeat/DESIGN.md``。
"""

from __future__ import annotations

from .config import HeartbeatServerConfig
from .plugin import HeartbeatServerPlugin
from .registry import (
    MAX_NODES,
    NODE_STATUS_EVENT,
    HeartbeatServerError,
    NodeRecord,
    NodeRegistry,
    NodeStatus,
)

__all__: list[str] = [
    "MAX_NODES",
    "NODE_STATUS_EVENT",
    "HeartbeatServerConfig",
    "HeartbeatServerError",
    "HeartbeatServerPlugin",
    "NodeRecord",
    "NodeRegistry",
    "NodeStatus",
]
