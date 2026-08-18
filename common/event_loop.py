"""事件循环工厂（Windows 兼容）。

aiomqtt（底层 paho-mqtt）依赖 selector 事件循环的 ``add_reader`` /
``add_writer``，而 Windows 默认的 ``ProactorEventLoop`` 不实现这两个方法，
会抛 ``NotImplementedError``。

Python 3.14 起 ``asyncio.get_event_loop_policy`` / ``set_event_loop_policy``
已弃用（3.16 移除），因此不再通过 policy 切换，而是直接提供 ``loop_factory``：

- Agent：``asyncio.run(main, loop_factory=selector_loop_factory)``；
- Master：``uvicorn.run(..., loop="common.event_loop:selector_loop_factory")``。

``selector_loop_factory`` 在 Windows 下直接实例化 ``asyncio.SelectorEventLoop``
（不弃用），其余平台返回 ``asyncio.new_event_loop()``（POSIX 默认即 selector）。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Coroutine, TypeVar


T = TypeVar("T")


def selector_loop_factory(use_subprocess: bool = False) -> asyncio.AbstractEventLoop:
    """返回 selector 事件循环（Windows 下 MQTT 兼容）。"""
    del use_subprocess  # 兼容 uvicorn 的 loop factory 调用约定。
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def run_with_selector(coro: Coroutine[object, object, T]) -> T:
    """使用 selector 事件循环运行协程，避免切换已弃用的全局 policy。"""
    loop = selector_loop_factory()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
            asyncio.set_event_loop(None)
