"""串口 resource plugin。"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from aetp_protocol.capabilities import ResourceCapability, ResourceHealth, SerialCapability, SerialPortCapability
from aetp_protocol.execution import PlanResourceBinding
from aetp_protocol.ids import stable_id
from aetp_protocol.resource import ResourceActivationError, ResourceDiscoveryError

from .base import ConfiguredResourceProvider, ResourceHook

DEFAULT_SERIAL_MAP_FILE = "serial_ports.json"


class SerialResourceProvider(ConfiguredResourceProvider):
    """读取功能名到端口号映射，并在发现/激活时检查端口存在性。"""

    provider_id = "org.aetp.serial-resource"
    resource_type = "serial"

    def __init__(
        self,
        serial_map_file: str | Path | None,
        *,
        port_exists: Callable[[str], bool] | None = None,
        activate_hook: ResourceHook | None = None,
        deactivate_hook: ResourceHook | None = None,
    ) -> None:
        self._serial_map_file = Path(serial_map_file) if serial_map_file is not None else None
        self._port_exists = port_exists or port_exists_on_host
        super().__init__(
            self.resource_type,
            self.provider_id,
            activate_hook=activate_hook,
            deactivate_hook=deactivate_hook,
        )

    def discover(self) -> tuple[ResourceCapability, ...]:
        path = self._serial_map_file
        if path is None or not path.exists():
            self._resources = {}
            return ()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ResourceDiscoveryError(f"串口映射文件无效: {path}") from exc
        if not isinstance(raw, dict):
            raise ResourceDiscoveryError("串口映射文件必须是 JSON 对象")
        resources = tuple(
            ResourceCapability(
                resource_id=stable_id(f"{self.provider_id}:{function}:{port}"),
                provider_id=self.provider_id,
                resource_type=self.resource_type,
                channel=port,
                function=function,
                labels={"source": "serial"},
                properties={"port": port},
                health=(
                    ResourceHealth.READY
                    if self._port_exists(port)
                    else ResourceHealth.UNAVAILABLE
                ),
            )
            for function, port in raw.items()
            if isinstance(function, str)
            and function
            and isinstance(port, str)
            and port
        )
        self._validate_resources(resources)
        self._resources = {resource.resource_id.root: resource for resource in resources}
        return resources

    async def activate(self, binding: PlanResourceBinding) -> None:
        resources = {resource.resource_id.root: resource for resource in self.discover()}
        resource = resources.get(binding.resource_id.root)
        if resource is None:
            raise ResourceActivationError(f"串口资源不存在: {binding.resource_id.root}")
        if resource.channel is None or not self._port_exists(resource.channel):
            raise ResourceActivationError(f"串口已断开: {resource.channel}")
        await super().activate(binding)


def scan_serial_ports(
    serial_map_file: str | Path | None,
    *,
    port_exists: Callable[[str], bool] | None = None,
) -> SerialCapability | None:
    """供旧能力适配层调用的串口扫描结果。"""
    provider = SerialResourceProvider(serial_map_file, port_exists=port_exists)
    try:
        resources = provider.discover()
    except ResourceDiscoveryError:
        return None
    if not resources:
        return None
    return SerialCapability(
        ports=tuple(
            SerialPortCapability(
                function=resource.function or "",
                port=resource.channel or "",
                enabled=resource.health is ResourceHealth.READY,
            )
            for resource in resources
        )
    )


def resolve_serial_map(serial_map_file: str | Path | None) -> Path | None:
    if serial_map_file:
        return Path(serial_map_file)
    candidate = Path(DEFAULT_SERIAL_MAP_FILE)
    return candidate if candidate.exists() else None


def port_exists_on_host(port: str) -> bool:
    try:
        if os.name == "nt":
            return os.path.exists(port) or os.path.exists(f"\\\\.\\{port}")
        return os.path.exists(port)
    except OSError:
        return False


def serial_fingerprint(serial_map_file: str | Path | None) -> tuple[tuple[str, str, bool], ...]:
    path = resolve_serial_map(serial_map_file)
    if path is None or not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ()
    if not isinstance(raw, dict):
        return ()
    return tuple(
        (function, port, port_exists_on_host(port))
        for function, port in raw.items()
        if isinstance(function, str) and function and isinstance(port, str) and port
    )


__all__ = [
    "DEFAULT_SERIAL_MAP_FILE",
    "SerialResourceProvider",
    "port_exists_on_host",
    "resolve_serial_map",
    "scan_serial_ports",
    "serial_fingerprint",
]
