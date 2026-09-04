"""Agent 优雅重启协调器。

进程内自重启不应在维护/插件同步消息处理器里直接 ``os.execv``（会跳过
``AgentRuntime.stop()`` 的清理：MQTT graceful disconnect、outbox/心跳/日志 spool
停止、shutdown presence）。本协调器把"请求重启"与"真正 execv"解耦：

- ``request_restart()`` 由维护控制器/插件同步控制器调用（同步，只置位事件 + 日志）；
- 主入口 ``_run`` 在 ``shutdown_event`` 与 ``restart`` 事件上等待，任一触发后先
  ``await runtime.stop()`` 完成优雅收尾，再 ``os.execv`` 重新拉起自身。

这样重启过程与 SIGTERM 优雅关闭走同一条收尾路径，只差最后 execv 而非退出。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger(__name__)


class AgentRestartCoordinator:
    """记录"是否请求了优雅重启"，供主入口在收尾后重新拉起进程。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def request_restart(self) -> None:
        """请求一次优雅重启（幂等）：置位事件，主入口据此先 stop 再 execv。"""
        if not self._event.is_set():
            logger.info("Agent 收到重启请求，将先优雅收尾再重新拉起进程")
        self._event.set()

    def requested(self) -> bool:
        """是否已请求重启。"""
        return self._event.is_set()

    async def wait_for_restart(self) -> None:
        """等待重启请求（可被取消）。"""
        await self._event.wait()


def reexec_process() -> None:
    """用当前解释器替换 Agent 进程（在 runtime.stop() 之后调用）。

    启动参数由部署器/命令行提供：源码运行 ``-m agent.main``，冻结运行直接用
    ``sys.executable``。execv 不返回。
    """
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
    else:
        os.execv(sys.executable, [sys.executable, "-m", "agent.main", *sys.argv[1:]])


__all__ = ["AgentRestartCoordinator", "reexec_process"]
