"""Agent V2 ResourceProvider 生命周期端口。"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Protocol

from aetp_protocol.capabilities import ResourceCapability, ResourceHealth
from aetp_protocol.execution import PlanResourceBinding
from aetp_protocol.ids import stable_id

logger = logging.getLogger(__name__)


class ResourceActivationError(RuntimeError):
    """资源激活或释放边界错误。"""


class ResourceProvider(Protocol):
    """一个资源类型的 activate/deactivate 实现。"""

    resource_type: str

    def discover(self) -> tuple[ResourceCapability, ...]: ...

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

    @property
    def resource_types(self) -> frozenset[str]:
        """返回已注册的资源类型，用于能力快照决定旧扫描回退范围。"""
        return frozenset(self._providers)

    def discover(self) -> tuple[ResourceCapability, ...]:
        """收集所有 Provider 的当前资源和健康状态。"""
        resources: list[ResourceCapability] = []
        for provider in self._providers.values():
            discover = getattr(provider, "discover", None)
            if discover is None:
                continue
            try:
                discovered = tuple(discover())
            except Exception:
                logger.exception("ResourceProvider 发现失败: resource_type=%s", provider.resource_type)
                continue
            for resource in discovered:
                if resource.resource_type != provider.resource_type:
                    raise ResourceDiscoveryError(
                        f"Provider 资源类型不一致: expected={provider.resource_type} "
                        f"actual={resource.resource_type}"
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


class ResourceDiscoveryError(RuntimeError):
    """资源发现结果不符合 Provider 契约。"""


ResourceHook = Callable[[ResourceCapability, PlanResourceBinding], Awaitable[None] | None]


class _ConfiguredResourceProvider:
    """基于配置/发现函数的资源 Provider 通用实现。"""

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
            if resource.resource_id.root in seen:
                raise ResourceDiscoveryError(f"资源 ID 重复: {resource.resource_id.root}")
            seen.add(resource.resource_id.root)


class CanResourceProvider(_ConfiguredResourceProvider):
    """CAN Provider；默认读取 Agent 资源配置，也可注入厂商发现适配器。"""

    def __init__(
        self,
        *,
        resources: Iterable[ResourceCapability] = (),
        discoverer: Callable[[], Iterable[ResourceCapability]] | None = None,
        activate_hook: ResourceHook | None = None,
        deactivate_hook: ResourceHook | None = None,
    ) -> None:
        configured_resources = tuple(resources)
        if discoverer is None and not configured_resources:
            discoverer = _discover_can_resources
        super().__init__(
            "can",
            "agent.can",
            resources=configured_resources,
            discoverer=discoverer,
            activate_hook=activate_hook,
            deactivate_hook=deactivate_hook,
        )


class PowerResourceProvider(_ConfiguredResourceProvider):
    """电源 Provider；物理继电器/串口控制通过 hook 注入。"""

    def __init__(
        self,
        *,
        resources: Iterable[ResourceCapability] = (),
        discoverer: Callable[[], Iterable[ResourceCapability]] | None = None,
        activate_hook: ResourceHook | None = None,
        deactivate_hook: ResourceHook | None = None,
    ) -> None:
        super().__init__(
            "power",
            "agent.power",
            resources=resources,
            discoverer=discoverer,
            activate_hook=activate_hook,
            deactivate_hook=deactivate_hook,
        )


class SerialResourceProvider(_ConfiguredResourceProvider):
    """串口 Provider；从功能到端口映射发现并在激活时复核端口存在。"""

    def __init__(
        self,
        serial_map_file: str | Path | None = None,
        *,
        port_exists: Callable[[str], bool] | None = None,
        activate_hook: ResourceHook | None = None,
        deactivate_hook: ResourceHook | None = None,
    ) -> None:
        self._serial_map_file = Path(serial_map_file) if serial_map_file is not None else None
        self._port_exists = port_exists or _port_exists
        super().__init__(
            "serial",
            "agent.serial",
            activate_hook=activate_hook,
            deactivate_hook=deactivate_hook,
        )

    def discover(self) -> tuple[ResourceCapability, ...]:
        if self._serial_map_file is None or not self._serial_map_file.exists():
            self._resources = {}
            return ()
        try:
            raw = json.loads(self._serial_map_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ResourceDiscoveryError(f"串口映射文件无效: {self._serial_map_file}") from exc
        if not isinstance(raw, dict):
            raise ResourceDiscoveryError("串口映射文件必须是 JSON 对象")
        resources = tuple(
            ResourceCapability(
                resource_id=stable_id(f"agent.serial:{function}:{port}"),
                provider_id=self.provider_id,
                resource_type=self.resource_type,
                channel=port,
                function=function,
                labels={"source": "serial"},
                properties={"port": port},
                health=ResourceHealth.READY if self._port_exists(port) else ResourceHealth.UNAVAILABLE,
            )
            for function, port in raw.items()
            if isinstance(function, str) and function and isinstance(port, str) and port
        )
        self._validate_resources(resources)
        self._resources = {resource.resource_id.root: resource for resource in resources}
        return resources

    async def activate(self, binding: PlanResourceBinding) -> None:
        resources = {resource.resource_id.root: resource for resource in self.discover()}
        resource = resources.get(binding.resource_id.root)
        if resource is None:
            raise ResourceActivationError(
                f"资源不存在: {binding.resource_id.root} ({self.resource_type})"
            )
        if resource.channel is None or not self._port_exists(resource.channel):
            raise ResourceActivationError(f"串口已断开: {resource.channel}")
        await super().activate(binding)


def _port_exists(port: str) -> bool:
    if os.name == "nt":
        return os.path.exists(port) or os.path.exists(f"\\\\.\\{port}")
    return os.path.exists(port)


def _discover_can_resources() -> tuple[ResourceCapability, ...]:
    """从现有 Vector 扫描结果生成统一 CAN 资源能力。"""
    try:
        from agent.application.services.capability_loader import scan_vehicle

        vehicle = scan_vehicle()
    except Exception:
        logger.exception("CAN Provider 扫描失败")
        return ()
    if vehicle is None:
        return ()
    resources: list[ResourceCapability] = []
    for vendor in vehicle.vendors:
        for bus in vendor.buses:
            for channel in bus.channels:
                resources.append(
                    ResourceCapability(
                        resource_id=stable_id(
                            f"agent.can:{vendor.name}:{bus.bus_type}:{channel.name}"
                        ),
                        provider_id="agent.can",
                        resource_type=bus.bus_type,
                        vendor=vendor.name,
                        model=channel.hardware_model,
                        channel=channel.name,
                        labels={"source": "vector"},
                        properties={},
                        health=(
                            ResourceHealth.READY
                            if channel.enabled
                            else ResourceHealth.UNAVAILABLE
                        ),
                    )
                )
    return tuple(resources)


__all__ = [
    "CanResourceProvider",
    "PowerResourceProvider",
    "ResourceActivationError",
    "ResourceDiscoveryError",
    "ResourceProvider",
    "ResourceProviderRegistry",
    "SerialResourceProvider",
]
