"""Agent Resource Provider 公共 SPI。"""

from __future__ import annotations

from typing import Protocol

from .capabilities import ResourceCapability
from .execution import PlanResourceBinding


class ResourceProviderError(RuntimeError):
    """Resource Provider 业务边界错误。"""


class ResourceActivationError(ResourceProviderError):
    """资源激活或释放失败。"""


class ResourceDiscoveryError(ResourceProviderError):
    """资源发现结果不符合 Provider 契约。"""


class ResourceProvider(Protocol):
    """Agent 侧资源插件必须实现的生命周期接口。"""

    resource_type: str
    provider_id: str

    def discover(self) -> tuple[ResourceCapability, ...]: ...

    async def activate(self, binding: PlanResourceBinding) -> None: ...

    async def deactivate(self, binding: PlanResourceBinding) -> None: ...


__all__ = [
    "ResourceActivationError",
    "ResourceDiscoveryError",
    "ResourceProvider",
    "ResourceProviderError",
]
