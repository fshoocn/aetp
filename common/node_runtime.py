"""节点运行时：主 / 从入口共用的装配与退出流程。

主节点（``master/main.py``）与从节点（``slave/main.py``）的差别只有四点：
节点类型 :class:`NodeKind`、Web 配置、插件安装目录（节点数据）、预装插件源列表。
其余装配完全一致：

1. 创建 cordis 内核并加载 :class:`WebApiPlugin`（HTTP 服务）；
2. 加载 :class:`PluginManagerPlugin`（安装能力 ``ctx.plugins``，``install_root``
   指向本节点的安装目录）；
3. 逐个「确保安装并启用」 ``plugin_sources`` 里的插件源目录（幂等）——
   传空列表即为纯 API（无头）节点；
4. :func:`run_node` 额外负责打印地址、等待 Ctrl+C / SIGTERM 并有序退出。

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
import json
import signal
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import cast

from cordis_port import Context

from common import cordis_utils
from common.plugins.webapi import WebApiPlugin
from common.plugins.webapi.plugin_manager_plugin import PluginManagerPlugin


class NodeKind(StrEnum):
    """节点类型。

    装配时注册为 ``node_kind`` 服务，插件声明 ``inject = ["node_kind"]``
    后即可经 ``ctx.node_kind`` 读取（cordis 的属性访问走服务解析，普通
    属性不沿上下文链下发）——未来的通信 / 分发插件据此决定行为
    （如主节点聚合、从节点汇报）。
    """

    MASTER = "master"
    SLAVE = "slave"


def source_plugin_id(source: Path) -> str:
    """从插件源目录的 ``plugin.json`` 读取插件 id。"""
    manifest_path = Path(source) / "plugin.json"
    manifest: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TypeError(f"plugin.json 根值必须是对象：{manifest_path}")
    plugin_id = cast(dict[str, object], manifest).get("id")
    if not isinstance(plugin_id, str) or not plugin_id:
        raise ValueError(f"插件源缺少有效 id：{manifest_path}")
    return plugin_id


async def ensure_installed(ctx: Context, source: Path) -> str:
    """确保插件源在本节点安装并启用（幂等），返回插件 id。"""
    plugins = ctx.plugins
    plugin_id = source_plugin_id(Path(source))
    if plugin_id not in {item["id"] for item in await plugins.list_plugins()}:
        await plugins.install_source(source)
    await plugins.enable(plugin_id)
    return plugin_id


async def assemble_node(
    *,
    kind: NodeKind,
    node_name: str,
    web_config: dict[str, object],
    install_root: Path,
    plugin_sources: Iterable[Path] = (),
) -> Context:
    """装配一个节点并返回内核（入口与测试共用）。

    参数:
        kind: 节点类型，注册为 ``node_kind`` 服务供插件读取。
        node_name: 节点名（日志与启动提示用）。
        web_config: 传给 ``WebApiPlugin`` 的配置（host / port 等）。
        install_root: 本节点的插件安装目录（节点数据）。
        plugin_sources: 启动时确保安装并启用的插件源目录列表。
    """
    ctx: Context = Context()
    cordis_utils.set_ctx(ctx)
    ctx.provide("node_kind", kind)

    await ctx.plugin(WebApiPlugin, web_config)
    await ctx.plugin(PluginManagerPlugin, {"install_root": str(install_root)})
    for source in plugin_sources:
        await ensure_installed(ctx, Path(source))

    ctx.logger.info("%s 装配完成（安装目录 %s）", node_name, install_root)
    return ctx


async def run_node(
    *,
    kind: NodeKind,
    node_name: str,
    web_config: dict[str, object],
    install_root: Path,
    plugin_sources: Iterable[Path] = (),
) -> None:
    """装配节点、打印地址，阻塞到 Ctrl+C / SIGTERM 后有序退出。

    **为什么需要「阻塞」**：web 服务跑在后台线程里，主协程一旦返回，
    ``asyncio.run`` 结束、进程退出，线程被连带杀掉。
    """
    ctx = await assemble_node(
        kind=kind,
        node_name=node_name,
        web_config=web_config,
        install_root=install_root,
        plugin_sources=plugin_sources,
    )

    address = ctx.webapi.address()
    ctx.logger.info("已就绪，访问 %s", address)
    print(f"\n  {node_name}已启动：{address}\n  按 Ctrl+C 退出\n", flush=True)

    stop, restore_signal_handlers = _install_stop_handler(asyncio.get_running_loop())
    try:
        await stop.wait()
    finally:
        restore_signal_handlers()

    # 有序退出：卸载全部插件，触发各自清理（web 服务停止、端口释放）
    ctx.logger.info("正在停止…")
    await cordis_utils.unload_all(ctx)
    print("\n  已退出", flush=True)


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


__all__: list[str] = [
    "NodeKind",
    "assemble_node",
    "ensure_installed",
    "run_node",
    "source_plugin_id",
]
