"""独立交付的心跳插件（``plugins/heartbeat``）。

向主模块周期性发送心跳，维护主模块在线状态感知：

* :mod:`.config` —— ``[heartbeat]`` 配置节（主模块地址、间隔、超时、退避上限）；
* :mod:`.capabilities` —— 能力信封契约（各插件挂载能力信息的统一格式）；
* :mod:`.sender` —— 传输层（标准库 urllib，异常统一折算为 ``HeartbeatSendError``）；
* :mod:`.monitor` —— 连接状态机与指数退避重试；
* :mod:`.plugin` —— cordis 插件入口（``plugin.json`` 的 entrypoint）。

主模块侧（接收心跳、从模块管理页面）的设计文档见
``master/heartbeat/DESIGN.md``。
"""

from .capabilities import (
    CAP_EVENT,
    Capability,
    capabilities_digest,
    capability,
    parse_capability,
    select_capabilities,
)
from .config import HeartbeatConfig
from .monitor import HeartbeatMonitor, HeartbeatSnapshot, MasterState
from .plugin import SEQ_MAX, STATUS_EVENT, HeartbeatPlugin, next_seq
from .sender import HeartbeatSender, HeartbeatSendError

__all__: list[str] = [
    "CAP_EVENT",
    "SEQ_MAX",
    "STATUS_EVENT",
    "Capability",
    "HeartbeatConfig",
    "HeartbeatMonitor",
    "HeartbeatPlugin",
    "HeartbeatSendError",
    "HeartbeatSender",
    "HeartbeatSnapshot",
    "MasterState",
    "capabilities_digest",
    "capability",
    "next_seq",
    "parse_capability",
    "select_capabilities",
]
