"""Master 专用的 asyncio 事件循环工厂。

uvicorn 在 Windows 上默认使用 ProactorEventLoop（见 uvicorn.loops.asyncio），
而 paho-mqtt 依赖 loop.add_reader/add_writer，Proactor 不支持导致
NotImplementedError。

用法：uvicorn.run(..., loop="master.loop_factory:selector_loop_factory")
"""

from __future__ import annotations

import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """创建并返回一个 SelectorEventLoop 实例。

    Windows 默认的 ProactorEventLoop 不支持 MQTT 客户端的
    loop.add_reader / loop.add_writer 接口（会抛出 NotImplementedError）。
    因此必须显式使用 SelectorEventLoop。

    uvicorn 的 ``loop`` 参数要求本函数作为 asyncio.Runner 的 loop_factory
    直接调用，所以必须返回事件循环实例而非类。
    """

    return asyncio.SelectorEventLoop()
