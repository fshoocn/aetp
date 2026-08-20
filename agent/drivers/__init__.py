"""Agent 硬件驱动（插件）。

硬件驱动是品牌/协议差异的唯一所在地：每种硬件类型（程控电源、继电器、
CAN 卡、示波器）实现一个 Driver，负责探测、占用、配置与释放。Master 只
按资源类型与标签选择，不感知具体品牌。
"""

from .base import Driver, ResourceMeta
from .registry import DriverRegistry

__all__ = ["Driver", "DriverRegistry", "ResourceMeta"]
