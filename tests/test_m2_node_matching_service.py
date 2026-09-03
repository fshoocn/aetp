"""M2 NodeMatchingService 应用层测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from aetp_protocol.capabilities import (
    AgentMaintenanceState,
    ExecutorCapability,
    NodeCapabilitySnapshot,
    PluginInventoryItem,
)
from aetp_protocol.execution import ExecutionRequirement, PluginRequirement
from aetp_protocol.ids import BusinessId, CapabilityName, PluginId, SemVer, SessionId, Sha256, VersionRange
from aetp_protocol.plugin_types import PluginAvailability, PluginPoint

from master.domain.enums import NodeStatus
from master.domain.models import Node, NodeSession

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
PLUGIN_ID = PluginId("org.example.executor")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _seed(container) -> None:
    with container.uow_factory()() as uow:
        node = uow.nodes.save(
            Node(
                id=None,
                node_id=NODE_ID.root,
                name="Bench 01",
                hostname="bench-01",
                status=NodeStatus.ONLINE,
                online=True,
                enabled=True,
            )
        )
        assert node.id is not None
        uow.node_sessions.create(
            NodeSession(
                node_pk=node.id,
                node_id=NODE_ID.root,
                session_id=SESSION_ID.root,
                client_id="agent-bench-01",
                connected_at=NOW,
            )
        )


def _snapshot() -> NodeCapabilitySnapshot:
    return NodeCapabilitySnapshot(
        schema_version=2,
        node_id=NODE_ID,
        session_id=SESSION_ID,
        revision=1,
        reported_at=NOW,
        maintenance_state=AgentMaintenanceState.IDLE,
        executors=(
            ExecutorCapability(
                plugin_id=PLUGIN_ID,
                version=SemVer("2.0.0"),
                capabilities=(CapabilityName("test.execute"),),
            ),
        ),
        plugin_inventory=(
            PluginInventoryItem(
                plugin_id=PLUGIN_ID,
                point=PluginPoint.EXECUTOR,
                version=SemVer("2.0.0"),
                archive_sha256=Sha256("a" * 64),
                availability=PluginAvailability.AVAILABLE,
                checked_at=NOW,
            ),
        ),
    )


def test_node_matching_service_uses_snapshot_and_online_projection(client) -> None:
    container = client.app.state.container
    _seed(container)
    assert container.capability_snapshot_service().accept(_snapshot()) is True
    requirement = ExecutionRequirement(
        executor=PluginRequirement(
            plugin_id=PLUGIN_ID,
            version=VersionRange(exact=SemVer("2.0.0")),
        )
    )

    matched = container.node_matching_service().match(requirement)
    assert len(matched) == 1
    assert matched[0].node_id == NODE_ID
    assert matched[0].matched is True

    with container.uow_factory()() as uow:
        node = uow.nodes.get_by_id(NODE_ID.root)
        assert node is not None
        node.online = False
        node.status = NodeStatus.OFFLINE
        uow.nodes.save(node)

    offline = container.node_matching_service().match(requirement)
    assert offline[0].matched is False
    assert offline[0].failures[0].root == "AGENT_OFFLINE"
