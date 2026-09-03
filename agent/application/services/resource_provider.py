"""Agent  ResourceProvider SPI 的注册与生命周期编排。"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from aetp_protocol.capabilities import ResourceCapability
from aetp_protocol.execution import PlanResourceBinding
from aetp_protocol.resource import ResourceActivationError, ResourceDiscoveryError, ResourceProvider

logger = logging.getLogger(__name__)


class ResourceProviderRegistry:
    """按资源类型注册和编排 Agent ResourceProvider。"""

    def __init__(self, providers: Iterable[ResourceProvider] = ()) -> None:
        self._providers: dict[str, list[ResourceProvider]] = {}
        self._provider_ids: set[str] = set()
        for provider in providers:
            self.register(provider)

    def register(self, provider: ResourceProvider) -> None:
        resource_type = provider.resource_type.strip()
        if not resource_type:
            raise ValueError("ResourceProvider resource_type 不能为空")
        provider_id = _provider_id(provider)
        if provider_id in self._provider_ids:
            raise ValueError(f"ResourceProvider 已注册: {provider_id}")
        self._providers.setdefault(resource_type, []).append(provider)
        self._provider_ids.add(provider_id)

    def get(self, resource_type: str) -> ResourceProvider | None:
        """返回该资源类型的首个 Provider，兼容单 Provider 调用方。"""
        providers = self._providers.get(resource_type, [])
        return providers[0] if providers else None

    @property
    def resource_types(self) -> frozenset[str]:
        """返回已注册的资源类型。"""
        return frozenset(self._providers)

    def discover(self) -> tuple[ResourceCapability, ...]:
        """收集所有 Provider 的当前资源和健康状态。"""
        resources: list[ResourceCapability] = []
        for providers in self._providers.values():
            for provider in providers:
                try:
                    discovered = tuple(provider.discover())
                except Exception:
                    logger.exception(
                        "ResourceProvider 发现失败: provider=%s",
                        _provider_id(provider),
                    )
                    continue
                for resource in discovered:
                    if resource.resource_type != provider.resource_type:
                        raise ResourceDiscoveryError(
                            f"Provider 资源类型不一致: expected={provider.resource_type} "
                            f"actual={resource.resource_type}"
                        )
                    provider_id = getattr(provider, "provider_id", None)
                    if provider_id is not None and resource.provider_id != provider_id:
                        raise ResourceDiscoveryError(
                            f"Provider 身份不一致: expected={provider_id} actual={resource.provider_id}"
                        )
                resources.extend(discovered)
        return tuple(resources)

    def require(self, resource_type: str) -> ResourceProvider:
        provider = self.get(resource_type)
        if provider is None:
            raise ResourceActivationError(f"未注册 ResourceProvider: {resource_type}")
        return provider

    async def activate(self, bindings: Iterable[PlanResourceBinding]) -> tuple[PlanResourceBinding, ...]:
        """按顺序激活全部资源，失败时回滚已激活资源。"""
        activated: list[PlanResourceBinding] = []
        try:
            for binding in bindings:
                provider = self._provider_for_binding(binding)
                try:
                    await provider.activate(binding)
                except Exception as exc:  # noqa: BLE001 - provider 边界统一映射
                    raise ResourceActivationError(
                        f"资源激活失败: {binding.resource_id.root} ({binding.resource_type})"
                    ) from exc
                activated.append(binding)
        except Exception:
            await self.deactivate(reversed(activated))
            raise
        return tuple(activated)

    async def deactivate(self, bindings: Iterable[PlanResourceBinding]) -> None:
        """反向释放资源；释放失败继续清理并抛出统一错误。"""
        first_error: ResourceActivationError | None = None
        for binding in bindings:
            try:
                provider = self._provider_for_binding(binding)
            except ResourceActivationError as exc:
                if first_error is None:
                    first_error = exc
                continue
            try:
                await provider.deactivate(binding)
            except Exception as exc:  # noqa: BLE001 - cleanup 继续处理其余资源
                if first_error is None:
                    first_error = ResourceActivationError(
                        f"资源释放失败: {binding.resource_id.root} ({binding.resource_type})"
                    )
                    first_error.__cause__ = exc
        if first_error is not None:
            raise first_error

    def _provider_for_binding(self, binding: PlanResourceBinding) -> ResourceProvider:
        providers = self._providers.get(binding.resource_type, [])
        if not providers:
            raise ResourceActivationError(f"未注册 ResourceProvider: {binding.resource_type}")
        if len(providers) == 1:
            return providers[0]
        for provider in providers:
            try:
                resources = provider.discover()
            except Exception:
                continue
            if any(resource.resource_id.root == binding.resource_id.root for resource in resources):
                return provider
        raise ResourceActivationError(
            f"没有 Provider 持有资源: {binding.resource_id.root} ({binding.resource_type})"
        )


def _provider_id(provider: ResourceProvider) -> str:
    return str(getattr(provider, "provider_id", provider.resource_type))


__all__ = [
    "ResourceActivationError",
    "ResourceDiscoveryError",
    "ResourceProvider",
    "ResourceProviderRegistry",
]
