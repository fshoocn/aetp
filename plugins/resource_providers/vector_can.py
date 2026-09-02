"""Vector CAN/LIN/FlexRay/Ethernet resource plugin。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from aetp_protocol.capabilities import (
    HardwareChannel,
    ResourceCapability,
    ResourceHealth,
    VehicleBus,
    VehicleCapability,
    VehicleVendor,
)
from aetp_protocol.execution import PlanResourceBinding
from aetp_protocol.ids import stable_id
from aetp_protocol.resource import ResourceActivationError

from .base import ConfiguredResourceProvider, ResourceHook

logger = logging.getLogger(__name__)

_BUS_TYPE_ATTRS: tuple[tuple[str, str], ...] = (
    ("can", "can"),
    ("lin", "lin"),
    ("flexray", "flexray"),
    ("ethernet", "ethernet"),
)


class VectorCanResourceProvider(ConfiguredResourceProvider):
    """通过 Vector XL/py-canoe 发现车载通道资源。"""

    provider_id = "com.vector.can-resource"
    resource_type = "can"

    def __init__(
        self,
        *,
        resources: Iterable[ResourceCapability] = (),
        discoverer: Callable[[], Iterable[ResourceCapability]] | None = None,
        device_discoverer: Callable[[], list] | None = None,
        activate_hook: ResourceHook | None = None,
        deactivate_hook: ResourceHook | None = None,
    ) -> None:
        configured_resources = tuple(resources)
        self._device_discoverer = device_discoverer or _discover_vector_devices
        self._uses_device_discovery = discoverer is None and not configured_resources
        super().__init__(
            self.resource_type,
            self.provider_id,
            resources=configured_resources,
            discoverer=discoverer,
            activate_hook=activate_hook,
            deactivate_hook=deactivate_hook,
        )

    def discover(self) -> tuple[ResourceCapability, ...]:
        if not self._uses_device_discovery:
            return super().discover()
        vehicle = _vehicle_from_devices(self._device_discoverer())
        resources = _resources_from_vehicle(vehicle)
        self._validate_resources(resources)
        self._resources = {resource.resource_id.root: resource for resource in resources}
        return resources

    async def activate(self, binding: PlanResourceBinding) -> None:
        resources = {resource.resource_id.root: resource for resource in self.discover()}
        resource = resources.get(binding.resource_id.root)
        if resource is None:
            raise ResourceActivationError(f"CAN 资源不存在: {binding.resource_id.root}")
        await super().activate(binding)


def scan_vector_vehicle(
    device_discoverer: Callable[[], list] | None = None,
) -> VehicleCapability | None:
    """供旧能力适配层调用的 Vector 扫描结果。"""
    try:
        devices = (device_discoverer or _discover_vector_devices)()
    except Exception as exc:  # noqa: BLE001 - 硬件未安装不阻塞 Agent
        logger.warning("Vector 硬件扫描失败: %s", exc)
        return None
    return _vehicle_from_devices(devices)


def _discover_vector_devices() -> list:
    try:
        from py_canoe.helpers.vxlapi import VxlDriver
    except Exception as exc:  # noqa: BLE001 - py-canoe 可选依赖
        raise RuntimeError("py-canoe 未安装") from exc
    return VxlDriver().get_devices()


def _vehicle_from_devices(devices: list) -> VehicleCapability | None:
    buses = group_buses_by_type(devices)
    if not buses:
        return None
    return VehicleCapability(
        vendors=(VehicleVendor(name="vector", buses=tuple(buses)),),
    )


def _resources_from_vehicle(vehicle: VehicleCapability | None) -> tuple[ResourceCapability, ...]:
    if vehicle is None:
        return ()
    resources: list[ResourceCapability] = []
    for vendor in vehicle.vendors:
        for bus in vendor.buses:
            if bus.bus_type != "can":
                continue
            for channel in bus.channels:
                resources.append(
                    ResourceCapability(
                        resource_id=stable_id(
                            f"{VectorCanResourceProvider.provider_id}:{vendor.name}:"
                            f"{bus.bus_type}:{channel.name}"
                        ),
                        provider_id=VectorCanResourceProvider.provider_id,
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


def group_buses_by_type(devices: list) -> list[VehicleBus]:
    """把 Vector 设备通道按总线类型转换为能力模型。"""
    by_type: dict[str, list[HardwareChannel]] = {}
    for device in devices:
        model = getattr(device, "name", None) or None
        for channel in getattr(device, "channels", []):
            for bus_type, attribute in _BUS_TYPE_ATTRS:
                if getattr(channel, attribute, False):
                    by_type.setdefault(bus_type, []).append(
                        HardwareChannel(
                            name=channel_name(device, channel),
                            hardware_model=model,
                            enabled=True,
                        )
                    )
    return [
        VehicleBus(bus_type=bus_type, channels=tuple(by_type[bus_type]))
        for bus_type in ("can", "lin", "flexray", "ethernet")
        if by_type.get(bus_type)
    ]


def channel_name(device, channel) -> str:
    device_name = getattr(device, "name", "") or ""
    name = getattr(channel, "name", "") or ""
    if name:
        return name
    if device_name:
        return f"{device_name} {getattr(channel, 'hw_channel', 0)}"
    return f"ch{getattr(channel, 'hw_channel', 0)}"


def can_fingerprint() -> tuple[tuple[str, str, str], ...]:
    try:
        devices = _discover_vector_devices()
    except Exception:
        return ()
    return tuple(
        (bus_type, channel_name(device, channel), getattr(device, "name", "") or "")
        for device in devices
        for channel in getattr(device, "channels", [])
        for bus_type, attribute in _BUS_TYPE_ATTRS
        if getattr(channel, attribute, False)
    )


__all__ = [
    "VectorCanResourceProvider",
    "can_fingerprint",
    "channel_name",
    "group_buses_by_type",
    "scan_vector_vehicle",
]
