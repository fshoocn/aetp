"""独立交付的心跳插件（``slave_plugins/heartbeat``）。

向主模块周期性发送心跳，维护主模块在线状态感知：

* :mod:`.config` —— ``[heartbeat]`` 配置节（运行参数：间隔、超时、退避上限）；
* :mod:`common.capabilities` —— 能力信封契约（各插件挂载能力信息的统一格式，
  公共层提供，主 / 从两侧心跳插件同用）；
* :mod:`.monitor` —— 连接状态机与指数退避重试（``HeartbeatSendError`` 定义于此）；
* :mod:`.plugin` —— cordis 插件入口（``plugin.json`` 的 entrypoint）；
  心跳经 ``ctx.comm`` 上报（``heartbeat.beat``），主模块下行
  ``heartbeat.config`` / ``heartbeat.caps`` 由本插件接收应用。

主模块侧（接收心跳、从模块管理页面）的设计文档见
``master_plugins/heartbeat/DESIGN.md``。
"""

from common.capabilities import (
    CAP_EVENT,
    Capability,
    CapProblemAlerter,
    capabilities_digest,
    capability,
    parse_capability,
    select_capabilities,
)

from .config import HeartbeatConfig
from .monitor import HeartbeatMonitor, HeartbeatSendError, HeartbeatSnapshot, MasterState
from .plugin import SEQ_MAX, STATUS_EVENT, HeartbeatPlugin, next_seq

__all__: list[str] = [
    "CAP_EVENT",
    "SEQ_MAX",
    "STATUS_EVENT",
    "CapProblemAlerter",
    "Capability",
    "HeartbeatConfig",
    "HeartbeatMonitor",
    "HeartbeatPlugin",
    "HeartbeatSendError",
    "HeartbeatSnapshot",
    "MasterState",
    "capabilities_digest",
    "capability",
    "next_seq",
    "parse_capability",
    "select_capabilities",
]
