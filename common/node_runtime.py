"""节点运行时：主 / 从入口共用的装配与退出流程。

主节点（``master/main.py``）与从节点（``slave/main.py``）的差别只有四点：
节点类型 :class:`NodeKind`、Web 配置、插件安装目录（节点数据）、预装插件源列表。
其余装配完全一致：

1. 创建 cordis 内核并加载 :class:`AppConfigPlugin`（应用配置 ``ctx.appconfig``，
   读执行根目录的 ``config.ini``）与 :class:`WebApiPlugin`（HTTP 服务）；
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
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import cast

from cordis_port import Context

from common import cordis_utils
from common.plugins.appconfig import AppConfig, AppConfigPlugin, ConfigSection
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


#: ``[node]`` 节里允许的键（节点自身的配置）
_NODE_KEYS: frozenset[str] = frozenset({"kind", "name", "install_root", "plugins"})


@dataclass(frozen=True)
class NodeProfile:
    """节点 ``config.ini``（``[node]`` + ``[web]`` 两节）的解析结果。

    节点类型、节点名、安装目录、预装插件源与 web 配置全部来自配置文件。
    """

    kind: NodeKind
    name: str
    install_root: Path
    plugin_sources: tuple[Path, ...]
    web_config: dict[str, object]


def load_node_profile(config: AppConfig, *, base_dir: Path) -> NodeProfile:
    """从应用配置里读出本节点的节点配置（``[node]`` / ``[web]`` 两节）。

    参数:
        config: :meth:`AppConfig.from_file` 生成的配置类。
        base_dir: 相对路径（``install_root`` / ``plugins``）的解析基准，
            取 ``config.ini`` 所在目录（节点的执行根目录）。

    抛出:
        KeyError: 配置里没有 ``[node]`` 节。
        ValueError: ``kind`` 缺失 / 非法、``[node]`` 出现未知键、web 字段类型不对。
    """
    if "node" not in config.sections():
        raise KeyError("config.ini 缺少 [node] 节")
    section = config.section("node")
    unknown = [key for key in section if key not in _NODE_KEYS]
    if unknown:
        raise ValueError(
            f"config.ini 的 [node] 节有未知键：{', '.join(unknown)}"
            f"（可选：{', '.join(sorted(_NODE_KEYS))}）"
        )

    raw_kind = section.get("kind")
    if raw_kind is None:
        raise ValueError("config.ini 的 [node] 节缺少 kind（master / slave）")
    try:
        kind = NodeKind(str(raw_kind).strip().lower())
    except ValueError as exc:
        raise ValueError(
            f"kind 无效：{raw_kind!r}（可选 {' / '.join(NodeKind)}）"
        ) from exc

    raw_root = section.get("install_root")
    install_root = Path(raw_root) if raw_root else Path("plugins")
    if not install_root.is_absolute():
        install_root = base_dir / install_root

    sources: list[Path] = []
    for item in _split_list(section.get("plugins")):
        source = Path(item)
        sources.append(source if source.is_absolute() else base_dir / source)

    return NodeProfile(
        kind=kind,
        name=section.get("name") or str(kind),
        install_root=install_root,
        plugin_sources=tuple(sources),
        web_config=_web_config(config.section("web")),
    )


def _split_list(raw: str | None) -> list[str]:
    """把逗号分隔的 ini 值拆成列表（去掉空白与空项）。"""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _web_config(section: ConfigSection) -> dict[str, object]:
    """``[web]`` 节 → 传给 ``WebApiConfig`` 的配置（按类型转换）。

    未知键不在这里拦截，由 :class:`WebApiConfig` 的校验报错。
    """
    values: dict[str, object] = {}
    for key in section:
        if key == "port":
            values[key] = section.get_int(key)
        elif key == "access_log":
            values[key] = section.get_bool(key)
        elif key == "start_timeout":
            values[key] = section.get_float(key)
        elif key == "cors_origins":
            values[key] = _split_list(section.get(key))
        else:
            values[key] = section.get(key)
    return values


def source_plugin_id(source: Path) -> str:
    """从插件源（源目录或 zip 交付包）的 ``plugin.json`` 读取插件 id。"""
    source = Path(source)
    if source.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(source) as package:
                raw = package.read("plugin.json")
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            raise ValueError(f"插件包无法读取 plugin.json：{source}：{exc}") from exc
        manifest: object = json.loads(raw)
    else:
        manifest_path = source / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TypeError(f"plugin.json 根值必须是对象：{source}")
    plugin_id = cast(dict[str, object], manifest).get("id")
    if not isinstance(plugin_id, str) or not plugin_id:
        raise ValueError(f"插件源缺少有效 id：{source}")
    return plugin_id


async def ensure_installed(ctx: Context, source: Path) -> str:
    """确保插件源（源目录或 zip 交付包）在本节点安装并启用（幂等），返回插件 id。"""
    plugins = ctx.plugins
    path = Path(source)
    plugin_id = source_plugin_id(path)
    if plugin_id not in {item["id"] for item in await plugins.list_plugins()}:
        if path.suffix.lower() == ".zip":
            await plugins.install_archive(path.read_bytes())
        else:
            await plugins.install_source(path)
    await plugins.enable(plugin_id)
    return plugin_id


async def assemble_node(
    *,
    config_path: str | Path = "config.ini",
) -> Context:
    """装配一个节点并返回内核（入口与测试共用）。

    参数:
        config_path: 节点配置文件路径（默认 ``config.ini``；入口通常传
            ``<节点目录>/config.ini``，如 ``master/config.ini``）。

    节点类型、节点名、安装目录、预装插件源与 web 配置**全部**来自配置文件
    （见 :func:`load_node_profile`）。
    """
    config_file = Path(config_path).expanduser().resolve()
    profile = load_node_profile(
        AppConfig.from_file(config_file), base_dir=config_file.parent
    )

    ctx: Context = Context()
    cordis_utils.set_ctx(ctx)
    ctx.provide("node_kind", profile.kind)

    await ctx.plugin(AppConfigPlugin, {"path": str(config_file)})
    await ctx.plugin(WebApiPlugin, profile.web_config)
    await ctx.plugin(PluginManagerPlugin, {"install_root": str(profile.install_root)})
    for source in profile.plugin_sources:
        await ensure_installed(ctx, source)

    ctx.logger.info(
        "%s 装配完成（安装目录 %s）", profile.name, profile.install_root
    )
    return ctx


async def run_node(
    *,
    config_path: str | Path = "config.ini",
) -> None:
    """装配节点、打印地址，阻塞到 Ctrl+C / SIGTERM 后有序退出。

    **为什么需要「阻塞」**：web 服务跑在后台线程里，主协程一旦返回，
    ``asyncio.run`` 结束、进程退出，线程被连带杀掉。
    """
    config_file = Path(config_path).expanduser().resolve()
    profile = load_node_profile(
        AppConfig.from_file(config_file), base_dir=config_file.parent
    )
    ctx = await assemble_node(config_path=config_file)

    address = ctx.webapi.address()
    ctx.logger.info("已就绪，访问 %s", address)
    print(f"\n  {profile.name}已启动：{address}\n  按 Ctrl+C 退出\n", flush=True)

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
    "NodeProfile",
    "assemble_node",
    "ensure_installed",
    "load_node_profile",
    "run_node",
    "source_plugin_id",
]
