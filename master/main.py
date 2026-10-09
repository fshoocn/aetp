"""AETP 主节点入口（薄启动器）。

读取本目录的 ``master/config.ini`` 装配并运行：节点类型 / 名称 / 安装目录 /
预装插件源 / web 端口全部来自该配置文件。装配细节、信号处理与退出清理见
:mod:`common.node_runtime`。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

# 直接 ``python master/main.py`` 运行时，sys.path[0] 是 ``master/`` 而非仓库根，
# 会导致 ``common`` 无法导入。这里把仓库根补进 sys.path。
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.node_runtime import run_node


async def boot() -> None:
    """按 master/config.ini 装配并运行主节点。"""
    await run_node(config_path=Path(__file__).resolve().parent / "config.ini")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(boot())
