"""M6 CAN、串口和电源 ResourceProvider 测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aetp_protocol.capabilities import ResourceCapability, ResourceHealth
from aetp_protocol.execution import PlanResourceBinding
from aetp_protocol.ids import BusinessId, stable_id
from aetp_protocol.resource import ResourceActivationError

from agent.application.services.resource_provider import ResourceProviderRegistry
from plugins.resource_providers import (
    PowerResourceProvider,
    SerialResourceProvider,
    VectorCanResourceProvider,
)


def _binding(resource_id: BusinessId, resource_type: str, *, labels: dict[str, str] | None = None):
    return PlanResourceBinding(
        lease_id=stable_id(f"lease:{resource_id.root}"),
        resource_id=resource_id,
        resource_type=resource_type,
        labels=labels or {},
        lease_revision=1,
        expires_at="2026-09-02T09:00:00Z",
    )


def test_registry_discovers_can_and_power_from_provider_adapters(tmp_path: Path) -> None:
    can_resource = ResourceCapability(
        resource_id=stable_id("can:CAN1"),
        provider_id="com.vector.can-resource",
        resource_type="can",
        channel="CAN1",
        vendor="vector",
        model="VN1640",
        labels={"bus": "bench"},
        properties={"can_fd": True},
        health=ResourceHealth.READY,
    )
    power_resource = ResourceCapability(
        resource_id=stable_id("power:PSU1"),
        provider_id="org.aetp.power-resource",
        resource_type="power",
        channel="PSU1",
        function="bench_supply",
        health=ResourceHealth.READY,
    )
    registry = ResourceProviderRegistry(
        (
            VectorCanResourceProvider(discoverer=lambda: (can_resource,)),
            PowerResourceProvider(resources=(power_resource,)),
        )
    )

    resources = registry.discover()

    assert [(item.resource_type, item.channel) for item in resources] == [
        ("can", "CAN1"),
        ("power", "PSU1"),
    ]
    assert resources[0].labels["bus"] == "bench"
    assert resources[0].health.value == "ready"


def test_serial_provider_marks_missing_port_unavailable_and_rechecks_activation(tmp_path: Path) -> None:
    mapping = tmp_path / "serial.json"
    mapping.write_text(json.dumps({"relay": "COM20", "psu": "COM30"}), encoding="utf-8")
    present = {"COM20"}
    provider = SerialResourceProvider(mapping, port_exists=present.__contains__)
    events: list[str] = []

    resources = provider.discover()
    relay = next(item for item in resources if item.function == "relay")
    psu = next(item for item in resources if item.function == "psu")
    provider._activate_hook = lambda resource, _binding: events.append(resource.channel or "")

    asyncio.run(provider.activate(_binding(relay.resource_id, "serial")))
    assert relay.health.value == "ready"
    assert psu.health.value == "unavailable"
    assert events == ["COM20"]

    present.clear()
    with pytest.raises(ResourceActivationError, match="串口已断开"):
        asyncio.run(provider.activate(_binding(relay.resource_id, "serial")))


def test_provider_rejects_unavailable_resource_and_label_mismatch(tmp_path: Path) -> None:
    resource = ResourceCapability(
        resource_id=stable_id("can:CAN2"),
        provider_id="com.vector.can-resource",
        resource_type="can",
        channel="CAN2",
        labels={"bus": "test"},
        health=ResourceHealth.UNAVAILABLE,
    )
    provider = VectorCanResourceProvider(resources=(resource,))

    with pytest.raises(ResourceActivationError, match="资源不可用"):
        asyncio.run(provider.activate(_binding(resource.resource_id, "can")))

    ready_provider = VectorCanResourceProvider(
        resources=(resource.model_copy(update={"health": ResourceHealth.READY}),)
    )
    with pytest.raises(ResourceActivationError, match="资源标签不匹配"):
        asyncio.run(
            ready_provider.activate(
                _binding(resource.resource_id, "can", labels={"bus": "other"})
            )
        )
