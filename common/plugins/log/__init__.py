"""日志插件（``common/plugins/log``，主从同一份）：日志可见化出口。

对外契约::

    from common.plugins.log import LogPlugin

    await ctx.plugin(LogPlugin)      # 依赖 appconfig，读 ``[log]`` 节

业务代码照常 ``ctx.logger.info(...)``——本插件经 cordis **exporter 机制**
给日志系统挂真实出口（控制台 + 轮转文件，二期追加上报 / 查询 / 查看页），
随插件 fiber 卸载自动注销。设计与排期见 ``docs/日志方案-2026-10-19.md``。
"""

from __future__ import annotations

from .config import LogConfig
from .op import current_operation, log_operation, log_operation_async
from .plugin import LogPlugin
from .store import LogStore

__all__: list[str] = [
    "LogConfig",
    "LogPlugin",
    "LogStore",
    "current_operation",
    "log_operation",
    "log_operation_async",
]
