"""Agent 硬件驱动端口（§18.5/§9.5）。

驱动是品牌/协议差异的唯一所在地：每个硬件类型实现一个 Driver，负责
探测、占用、配置与释放。Master 只按资源类型与标签选择，不感知具体品牌。
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
