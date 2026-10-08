"""AETP 主入口。

职责只有两件事：创建 cordis 内核（唯一的 ``Context``）并加载插件；随后保持进程
存活直到收到中断信号。

**为什么需要「保持存活」**：web 插件把 HTTP 服务跑在后台线程里，一旦主协程结束，
``asyncio.run`` 返回、进程随即退出、线程被连带杀掉。所以 ``boot`` 末尾必须挂住，
不能直接返回。

**为什么用 ``signal.signal`` 而不是等 ``KeyboardInterrupt``**：后者看似更简洁
（``try: await ... finally: await 清理``），但实测不可靠 —— 当 ``boot()`` 被作为
任务运行时，``Ctrl+C`` 会让事件循环直接取消任务，``finally`` 里的 await 清理根本
不会执行，端口因此不会释放。改用 ``signal.signal`` 注册处理器：它在主线程同步
执行，只负责置位一个 ``asyncio.Event``（经 ``loop.call_soon_threadsafe`` 投递，
线程安全），主协程随后正常返回并完成清理。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType

# 直接 ``python master/main.py`` 运行时，sys.path[0] 是 ``master/`` 而非仓库根，
# 会导致 ``common`` / ``plugins`` 无法导入。这里把仓库根补进 sys.path。
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cordis_port import Context

from common import cordis_utils
from master.plugins.web import WebPlugin


def _install_stop_handler(
    loop: asyncio.AbstractEventLoop,
) -> tuple[asyncio.Event, Callable[[], None]]:
    """注册 Ctrl+C / SIGTERM 处理器，返回停止事件及其清理函数。

    ``signal.signal`` 只能用在主线程，这与本项目「单进程、主线程跑 asyncio」的
    结构相符。处理器内部只做一件事：用 ``call_soon_threadsafe`` 唤醒等待中的
    协程（``asyncio.Event.set`` 本身不是线程安全的，必须经线程安全入口投递）。
    """
    stop: asyncio.Event = asyncio.Event()
    previous: dict[
        int,
        signal.Handlers | int | Callable[[int, FrameType | None], None] | None,
    ] = {}

    def on_signal(_signum: int, _frame: FrameType | None) -> None:
        """信号处理器：只负责唤醒事件循环，绝不在此做清理。"""
        del _signum, _frame
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            previous[sig] = signal.signal(sig, on_signal)

    def restore() -> None:
        """恢复原信号处理器，避免影响后续代码。"""
        for sig, handler in previous.items():
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig, handler)

    return stop, restore


async def boot() -> None:
    """启动内核、加载插件，并阻塞直到收到中断信号。"""
    # 1. 内核：唯一 Context
    ctx: Context = Context()
    cordis_utils.set_ctx(ctx)

    # 2. Web 服务：应最先加载 —— 其他插件依赖它来注册 UI contributions 与 API
    await ctx.plugin(WebPlugin, {"port": 8080})

    # 3. 报告服务地址
    address = ctx.web.address()
    ctx.logger.info("已就绪，访问 %s", address)
    print(f"\n  平台已启动：{address}\n  按 Ctrl+C 退出\n", flush=True)

    # 4. 等待中断信号
    stop, restore_signal_handlers = _install_stop_handler(asyncio.get_running_loop())
    try:
        await stop.wait()
    finally:
        restore_signal_handlers()

    # 5. 有序退出：卸载全部插件，触发各自清理（web 服务停止、端口释放）
    ctx.logger.info("正在停止…")
    await cordis_utils.unload_all(ctx)
    print("\n  已退出", flush=True)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(boot())
