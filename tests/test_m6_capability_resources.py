"""M6 ResourceProvider 与 Agent 能力快照集成测试。"""

from __future__ import annotations

from pathlib import Path

from aetp_protocol.capabilities import NodeCapabilities, ResourceCapability, ResourceHealth
from aetp_protocol.ids import BusinessId, SessionId

from agent.application.services.capability_snapshot_service import AgentCapabilitySnapshotService
from agent.application.services.resource_provider import ResourceProviderRegistry
from agent.plugins.registry import PluginRegistry
from plugins.resource_providers import PowerResourceProvider, VectorCanResourceProvider

NODE_ID = BusinessId("01J000000000000000000000A0")
SESSION_ID = SessionId("session-00000100")


def test_capability_snapshot_uses_provider_resources_without_legacy_duplicates(tmp_path: Path) -> None:
    can_resource = ResourceCapability(
        resource_id=BusinessId("01J000000000000000000000B1"),
        provider_id="com.vector.can-resource",
        resource_type="can",
        channel="CAN1",
        vendor="vector",
        health=ResourceHealth.READY,
    )
    power_resource = ResourceCapability(
        resource_id=BusinessId("01J000000000000000000000B2"),
        provider_id="org.aetp.power-resource",
        resource_type="power",
        channel="PSU1",
        function="supply",
        health=ResourceHealth.READY,
    )
    providers = ResourceProviderRegistry(
        (
            VectorCanResourceProvider(discoverer=lambda: (can_resource,)),
            PowerResourceProvider(resources=(power_resource,)),
        )
    )
    service = AgentCapabilitySnapshotService(
        NODE_ID,
        SESSION_ID,
        PluginRegistry(tmp_path / "plugins"),
        capability_scanner=lambda: NodeCapabilities(),
        resource_providers=providers,
    )

    snapshot = service.build_snapshot()

    assert [(resource.resource_type, resource.channel) for resource in snapshot.resources] == [
        ("can", "CAN1"),
        ("power", "PSU1"),
    ]
    assert {resource.provider_id for resource in snapshot.resources} == {
        "com.vector.can-resource",
        "org.aetp.power-resource",
    }
