"""内置 Agent resource 插件共享的生命周期实现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from aetp_protocol.capabilities import ResourceCapability, ResourceHealth
from aetp_protocol.execution import PlanResourceBinding
from aetp_protocol.resource import ResourceActivationError, ResourceDiscoveryError

ResourceHook = Callable[[ResourceCapability, PlanResourceBinding], Awaitable[None] | None]


class ConfiguredResourceProvider:
    """资源插件的通用发现、健康检查和激活生命周期。"""

    def __init__(
        self,
        resource_type: str,
        provider_id: str,
        *,
        resources: Iterable[ResourceCapability] = (),
        discoverer: Callable[[], Iterable[ResourceCapability]] | None = None,
        activate_hook: ResourceHook | None = None,
        deactivate_hook: ResourceHook | None = None,
    ) -> None:
        self.resource_type = resource_type
        self.provider_id = provider_id
        self._resources = {resource.resource_id.root: resource for resource in resources}
        self._discoverer = discoverer
        self._activate_hook = activate_hook
        self._deactivate_hook = deactivate_hook
        self._active: set[str] = set()

    def discover(self) -> tuple[ResourceCapability, ...]:
        if self._discoverer is not None:
            discovered = tuple(self._discoverer())
            self._validate_resources(discovered)
            self._resources = {resource.resource_id.root: resource for resource in discovered}
        return tuple(self._resources.values())

    async def activate(self, binding: PlanResourceBinding) -> None:
        resources = {resource.resource_id.root: resource for resource in self.discover()}
        resource = resources.get(binding.resource_id.root)
        if resource is None:
            raise ResourceActivationError(
                f"资源不存在: {binding.resource_id.root} ({self.resource_type})"
            )
        if resource.health is not ResourceHealth.READY:
            raise ResourceActivationError(
                f"资源不可用: {binding.resource_id.root} ({resource.health.value})"
            )
        if any(resource.labels.get(key) != value for key, value in binding.labels.items()):
            raise ResourceActivationError(f"资源标签不匹配: {binding.resource_id.root}")
        if self._activate_hook is not None and binding.resource_id.root not in self._active:
            result = self._activate_hook(resource, binding)
            if result is not None:
                await result
        self._active.add(binding.resource_id.root)

    async def deactivate(self, binding: PlanResourceBinding) -> None:
        resource = self._resources.get(binding.resource_id.root)
        if resource is None:
            return
        if self._deactivate_hook is not None and binding.resource_id.root in self._active:
            result = self._deactivate_hook(resource, binding)
            if result is not None:
                await result
        self._active.discard(binding.resource_id.root)

    def _validate_resources(self, resources: Iterable[ResourceCapability]) -> None:
        seen: set[str] = set()
        for resource in resources:
            if resource.resource_type != self.resource_type:
                raise ResourceDiscoveryError(
                    f"资源 resource_type 不匹配: expected={self.resource_type} actual={resource.resource_type}"
                )
            if resource.provider_id != self.provider_id:
                raise ResourceDiscoveryError(
                    f"资源 provider_id 不匹配: expected={self.provider_id} actual={resource.provider_id}"
                )
            if resource.resource_id.root in seen:
                raise ResourceDiscoveryError(f"资源 ID 重复: {resource.resource_id.root}")
            seen.add(resource.resource_id.root)


__all__ = ["ConfiguredResourceProvider", "ResourceHook"]
