"""org.aetp.resource 插件 agent 入口：一次提供 serial/power/can 三种资源能力。

一个插件包提供三个 ResourceProvider（各自独立 provider_id 与 resource_type），
由 ``create_providers()`` 返回元组。Agent ``ResourceProviderResolver`` 会展开为多个
Provider 并注册进 ``ResourceProviderRegistry``。

串口映射：默认从本包固定位置 ``agent/serial_ports.json`` 读取（JSON
``{ "功能名": "端口号" }``）。用户可通过编辑**已安装插件**目录下该文件来自定义
功能名到端口的映射；文件不存在或无效时串口 Provider 发现为空（不报错）。
"""

from __future__ import annotations

from pathlib import Path

from aetp_protocol.resource import ResourceProvider

from .power import PowerResourceProvider
from .serial import SerialResourceProvider
from .vector_can import VectorCanResourceProvider


def create_providers() -> tuple[ResourceProvider, ...]:
    agent_dir = Path(__file__).resolve().parent
    serial_map = agent_dir / "serial_ports.json"
    return (
        SerialResourceProvider(serial_map),
        PowerResourceProvider(),
        VectorCanResourceProvider(),
    )


__all__ = ["create_providers"]
