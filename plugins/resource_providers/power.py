"""电源 resource plugin。"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from aetp_protocol.capabilities import ResourceCapability

from .base import ConfiguredResourceProvider, ResourceHook


class PowerResourceProvider(ConfiguredResourceProvider):
    """由电源适配器注入发现和控制实现，不读取 JSON 伪造硬件。"""

    provider_id = "org.aetp.power-resource"
    resource_type = "power"

    def __init__(
        self,
        *,
        resources: Iterable[ResourceCapability] = (),
        discoverer: Callable[[], Iterable[ResourceCapability]] | None = None,
        activate_hook: ResourceHook | None = None,
        deactivate_hook: ResourceHook | None = None,
    ) -> None:
        super().__init__(
            self.resource_type,
            self.provider_id,
            resources=resources,
            discoverer=discoverer,
            activate_hook=activate_hook,
            deactivate_hook=deactivate_hook,
        )


__all__ = ["PowerResourceProvider"]
