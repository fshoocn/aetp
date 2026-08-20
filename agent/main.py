"""Agent 命令行入口（P5.3）。

启动顺序由 ``AgentRuntime`` 统一编排：加载外置 .env → 初始化日志 →
创建容器 → 注册 MQTT handlers → 连接 → register outbox → register-ack
→ 心跳。Agent 关闭时按相反顺序释放资源。

用法：
    python -m agent.main
    python -m agent.main --env-file path/to/agent.env
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from common.event_loop import run_with_selector
from common.logging_config import configure_logging

from agent.config import configure
from agent.container import Container

logger = logging.getLogger(__name__)


async def _run(env_file: str | None) -> None:
    """初始化 AgentRuntime 并保持进程运行。"""
    settings = configure(env_file)
    log_path = configure_logging(
        settings.log_file,
        level=settings.log_level,
        console=settings.log_console,
    )
    container = Container()
    runtime = container.runtime()
    logger.info("Agent 日志文件: %s", log_path)
    await runtime.start()

    # 注册信号处理器：SIGINT/SIGTERM 触发优雅关闭
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("收到停止信号，Agent 正在关闭")
        shutdown_event.set()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
    else:
        # Windows 不支持 add_signal_handler，用 signal.signal + call_soon_threadsafe
        import threading

        def _win_handler(signum, frame):
            if not shutdown_event.is_set():
                loop.call_soon_threadsafe(_signal_handler)

        signal.signal(signal.SIGINT, _win_handler)
        signal.signal(signal.SIGTERM, _win_handler)

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        # 信号处理器通过 cancel() 触发时，等待 stop 完成后再传播
        pass
    finally:
        await runtime.stop()


def main() -> None:
    """解析 CLI 参数并运行 Agent。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        default=None,
        help="外置 Agent .env 路径（默认 agent/.env 或 exe 同目录/.env）",
    )
    args = parser.parse_args()
    run_with_selector(_run(args.env_file))


if __name__ == "__main__":
    main()
