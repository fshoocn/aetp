"""Agent 硬件驱动端口（§18.5/§9.5）。

驱动是品牌/协议差异的唯一所在地：每个硬件类型实现一个 Driver，负责
探测、占用、配置与释放。Master 只按资源类型与标签选择，不感知具体品牌。

本端口是**硬件访问抽象层**，供共享插件包 Agent 面在 ``execute`` 内调用
（P9.2）：插件通过 ``Driver.acquire`` 打开物理口、``configure`` 配置、
``release`` 释放。Agent 框架本身**不预占用**物理口——占用/释放的时机由插件
决定，占用状态的事实源是 Master（派发时设备标 ``busy``，终态释放）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ResourceMeta:
    """Agent 探测/配置产出的资源元数据。"""

    resource_type: str
    vendor: str | None = None
    model: str | None = None
    channel: str | None = None
    function: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)


class Driver(Protocol):
    """硬件驱动协议：探测 + 占用 + 配置 + 释放。"""

    resource_type: str

    def detect(self, connection: Mapping[str, Any]) -> ResourceMeta | None:
        """探测一个连接是否属于本驱动；属于则返回资源元数据，否则 None。"""
        ...

    def acquire(self, resource: ResourceMeta) -> None:
        """占用资源（初始化、锁定）。"""
        ...

    def configure(self, resource: ResourceMeta, params: Mapping[str, Any]) -> None:
        """配置资源（例如把切换开关切到指定通道）。"""
        ...

    def release(self, resource: ResourceMeta) -> None:
        """释放资源。"""
        ...
