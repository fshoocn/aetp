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
    try:
        await asyncio.Event().wait()
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
    try:
        run_with_selector(_run(args.env_file))
    except KeyboardInterrupt:
        logger.info("收到停止信号，Agent 正在关闭")


if __name__ == "__main__":
    main()
