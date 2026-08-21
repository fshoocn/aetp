"""Agent 硬件驱动注册表（品牌隔离）。

新硬件 = 新增一个 Driver 并注册；Master 不感知具体品牌。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import Driver, ResourceMeta


class DriverRegistry:
    """按资源类型登记驱动。"""

    def __init__(self) -> None:
        self._drivers: dict[str, Driver] = {}

    def register(self, driver: Driver) -> None:
        if driver.resource_type in self._drivers:
            raise ValueError(f"驱动已注册: {driver.resource_type}")
        self._drivers[driver.resource_type] = driver

    def get(self, resource_type: str) -> Driver | None:
        return self._drivers.get(resource_type)

    def require(self, resource_type: str) -> Driver:
        driver = self.get(resource_type)
        if driver is None:
            raise KeyError(f"未注册硬件驱动: {resource_type}")
        return driver

    def detect(self, resource_type: str, connection: Mapping[str, Any]) -> ResourceMeta | None:
        driver = self.get(resource_type)
        return None if driver is None else driver.detect(connection)
