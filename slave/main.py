"""AETP 从节点入口（无头执行节点）。

与主节点的差别只有三点：

1. Web 端口独立（默认 8081）；
2. 插件安装目录是**从节点自己的** ``slave/plugins``（节点数据）；
3. **不安装** ``webui`` —— 纯 API（无头）模式：页面路径返回 JSON 404、
   ``/api/web/ui`` 不存在；查看页面统一由主节点提供。

执行插件 / 节点代理插件的源文件放 ``slaveplugins/``，在 ``plugin_sources``
里列出即可随启动「安装并启用」（幂等）。主从通信（节点注册、心跳、插件分发、
测试数据回传）由后续的**通信插件**提供，届时同样经 ``plugin_sources`` 预装。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

# 直接 ``python slave/main.py`` 运行时，sys.path[0] 是 ``slave/`` 而非仓库根，
# 会导致 ``common`` 无法导入。这里把仓库根补进 sys.path。
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.node_runtime import run_node


async def boot() -> None:
    """按 slave/config.ini 装配并运行从节点。"""
    await run_node(config_path=Path(__file__).resolve().parent / "config.ini")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(boot())
