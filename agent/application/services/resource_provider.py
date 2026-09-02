"""Agent V2 ResourceProvider 生命周期端口。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from aetp_protocol.execution import PlanResourceBinding


class ResourceActivationError(RuntimeError):
    """资源激活或释放边界错误。"""


class ResourceProvider(Protocol):
    """一个资源类型的 activate/deactivate 实现。"""

    resource_type: str

    async def activate(self, binding: PlanResourceBinding) -> None: ...

    async def deactivate(self, binding: PlanResourceBinding) -> None: ...


class ResourceProviderRegistry:
    """按 PlanResourceBinding.resource_type 解析 Provider。"""

    def __init__(self, providers: Iterable[ResourceProvider] = ()) -> None:
        self._providers: dict[str, ResourceProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: ResourceProvider) -> None:
        if not provider.resource_type.strip():
            raise ValueError("ResourceProvider resource_type 不能为空")
        if provider.resource_type in self._providers:
            raise ValueError(f"ResourceProvider 已注册: {provider.resource_type}")
        self._providers[provider.resource_type] = provider

    def get(self, resource_type: str) -> ResourceProvider | None:
        return self._providers.get(resource_type)

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
                provider = self.require(binding.resource_type)
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
            provider = self.get(binding.resource_type)
            if provider is None:
                if first_error is None:
                    first_error = ResourceActivationError(
                        f"未注册 ResourceProvider: {binding.resource_type}"
                    )
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


__all__ = ["ResourceActivationError", "ResourceProvider", "ResourceProviderRegistry"]
