"""AETP 主节点入口。

按主节点形态调用 :func:`common.node_runtime.run_node` —— Web 配置
``{"port": 8080}``、安装目录 ``master/plugins``、预装 UI 插件源
``masterplugins/webui``。装配细节、信号处理与退出清理见
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

from common.node_runtime import NodeKind, run_node


async def boot() -> None:
    """按主节点形态装配并运行（UI 随启动预装，跳过 sources 即无头）。"""
    await run_node(
        kind=NodeKind.MASTER,
        node_name="主节点",
        web_config={"port": 8080},
        install_root=Path(__file__).resolve().parent / "plugins",
        plugin_sources=[_REPO_ROOT / "masterplugins" / "webui"],
    )


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(boot())
