from __future__ import annotations

from datetime import datetime, timezone

from aetp_protocol.capabilities import (
    DeviceRequirement,
    HardwareRequirements,
    NodeCapabilities,
    PhysicalDeviceCapability,
    SwitchConnection,
    SwitchPort,
)

from master.domain.enums import DeviceStatus, NodeStatus, ShardAttemptStatus
from master.domain.models import Device, Node, ShardAttempt
from master.domain.resources import (
    NodeSchedulingState,
    ResourceAllocator,
    SwitchRoute,
)
from master.domain.scheduler import ShardScheduler


def _node(
    node_id: str,
    *,
    devices: tuple[Device, ...] = (),
    online: bool = True,
    enabled: bool = True,
    last_seen_second: int = 0,
) -> NodeSchedulingState:
    node = Node(
        id=None,
        node_id=node_id,
        name=node_id,
        hostname=node_id,
        status=NodeStatus.ONLINE if online else NodeStatus.OFFLINE,
        online=online,
        enabled=enabled,
        capabilities=NodeCapabilities(),
        last_seen_at=datetime(2026, 1, 1, 0, 0, last_seen_second, tzinfo=timezone.utc),
        devices=list(devices),
    )
    return NodeSchedulingState(node=node)


def _device(
    device_id: str,
    *,
    status: DeviceStatus = DeviceStatus.ONLINE,
    online: bool = True,
    capability: PhysicalDeviceCapability | None = None,
) -> Device:
    return Device(
        id=None,
        device_id=device_id,
        node_id=None,
        name=device_id,
        status=status,
        online=online,
        capability=capability or PhysicalDeviceCapability(resource_type="generic"),
    )


def test_scheduler_waits_when_device_is_occupied() -> None:
    scheduler = ShardScheduler()
    candidates = [
        _node("device-busy", devices=(_device("device-a", status=DeviceStatus.BUSY),)),
        _node("available", devices=(_device("device-b"),)),
    ]

    selected = scheduler.select_node(
        requirements=HardwareRequirements(
            devices=(DeviceRequirement(resource_type="generic"),)
        ),
        candidates=candidates,
    )

    assert selected is not None
    assert selected.node_id == "available"


def test_scheduler_filters_offline_disabled_and_low_capability_nodes() -> None:
    scheduler = ShardScheduler()
    candidates = [
        _node("offline", online=False),
        _node("disabled", enabled=False),
        _node("available", devices=(_device("device-a"),)),
    ]

    selected = scheduler.select_node(
        requirements=HardwareRequirements(),
        candidates=candidates,
    )

    assert selected is not None
    assert selected.node_id == "available"


def test_scheduler_orders_by_active_load_queue_and_recency() -> None:
    scheduler = ShardScheduler()
    candidates = [
        _node("older", devices=(_device("device-a"),), last_seen_second=1),
        _node("recent", devices=(_device("device-b"),), last_seen_second=59),
        _node("another", devices=(_device("device-c"),), last_seen_second=59),
    ]

    ordered = scheduler.eligible_candidates(
        requirements=HardwareRequirements(),
        candidates=candidates,
    )

    assert [state.node.node_id for state in ordered] == ["another", "recent", "older"]


def test_failover_excludes_every_previously_attempted_node() -> None:
    scheduler = ShardScheduler()
    attempts = [
        ShardAttempt(
            attempt_id="attempt-1",
            shard_id="shard-1",
            attempt_no=1,
            node_id="node-a",
            status=ShardAttemptStatus.FAILED,
        ),
        ShardAttempt(
            attempt_id="attempt-2",
            shard_id="shard-1",
            attempt_no=2,
            node_id="node-b",
            status=ShardAttemptStatus.FAILED,
        ),
    ]

    selected = scheduler.select_failover_node(
        requirements=HardwareRequirements(),
        candidates=[
            _node("node-a", devices=(_device("device-a"),)),
            _node("node-b", devices=(_device("device-b"),)),
            _node("node-c", devices=(_device("device-c"),)),
        ],
        attempts=attempts,
    )

    assert selected is not None
    assert selected.node_id == "node-c"


def test_scheduler_respects_required_labels() -> None:
    scheduler = ShardScheduler()
    candidates = [
        _node(
            "wrong",
            devices=(
                _device(
                    "can-a",
                    capability=PhysicalDeviceCapability(
                        resource_type="can_channel", labels={"project": "P1"}
                    ),
                ),
            ),
        ),
        _node(
            "right",
            devices=(
                _device(
                    "can-b",
                    capability=PhysicalDeviceCapability(
                        resource_type="can_channel", labels={"project": "P3"}
                    ),
                ),
            ),
        ),
    ]
    requirements = HardwareRequirements(
        devices=(
            DeviceRequirement(
                resource_type="can_channel", required_labels={"project": "P3"}
            ),
        )
    )

    selected = scheduler.select_node(requirements=requirements, candidates=candidates)

    assert selected is not None
    assert selected.node_id == "right"


def test_allocator_prefers_matching_labels() -> None:
    allocator = ResourceAllocator()
    node = Node(
        id=None,
        node_id="n1",
        name="n1",
        hostname="n1",
        status=NodeStatus.ONLINE,
        online=True,
        enabled=True,
        devices=[
            _device(
                "can-a",
                capability=PhysicalDeviceCapability(
                    resource_type="can_channel", labels={"project": "P1"}
                ),
            ),
            _device(
                "can-b",
                capability=PhysicalDeviceCapability(
                    resource_type="can_channel", labels={"project": "P3"}
                ),
            ),
        ],
    )

    result = allocator.allocate(
        node,
        (
            DeviceRequirement(
                resource_type="can_channel", preferred_labels={"project": "P3"}
            ),
        ),
        frozenset(),
    )

    assert result is not None
    assert [device.device_id for device in result.devices] == ["can-b"]


def test_allocator_routes_via_switch_when_device_labels_mismatch() -> None:
    """单 CAN 卡 + 继电器切换：must 标签与设备自身不符时经端口匹配。"""

    allocator = ResourceAllocator()
    node = Node(
        id=None,
        node_id="n1",
        name="n1",
        hostname="n1",
        status=NodeStatus.ONLINE,
        online=True,
        enabled=True,
        devices=[
            _device(
                "can-1640-1",
                capability=PhysicalDeviceCapability(
                    resource_type="can_channel",
                    channel="can1",
                    connection=SwitchConnection(
                        switch_device_id="relay-board-1",
                        ports=(
                            SwitchPort(port="port1", labels={"project": "P1"}),
                            SwitchPort(port="port3", labels={"project": "P3"}),
                        ),
                    ),
                ),
            ),
        ],
    )

    result = allocator.allocate(
        node,
        (
            DeviceRequirement(
                resource_type="can_channel",
                required_labels={"project": "P3"},
                allow_switching=True,
            ),
        ),
        frozenset(),
    )

    assert result is not None
    assert [device.device_id for device in result.devices] == ["can-1640-1"]
    assert result.routes_by_device["can-1640-1"] == SwitchRoute(
        switch_device_id="relay-board-1", port="port3"
    )


def test_allocator_waits_when_switch_port_taken() -> None:
    """同一继电器端口被两个需求争用时整组排队。"""

    allocator = ResourceAllocator()
    capability = PhysicalDeviceCapability(
        resource_type="can_channel",
        channel="can1",
        connection=SwitchConnection(
            switch_device_id="relay-board-1",
            ports=(SwitchPort(port="port3", labels={"project": "P3"}),),
        ),
    )
    node = Node(
        id=None,
        node_id="n1",
        name="n1",
        hostname="n1",
        status=NodeStatus.ONLINE,
        online=True,
        enabled=True,
        devices=[_device("can-a", capability=capability)],
    )

    result = allocator.allocate(
        node,
        (
            DeviceRequirement(
                resource_type="can_channel",
                required_labels={"project": "P3"},
                allow_switching=True,
            ),
            DeviceRequirement(
                resource_type="can_channel",
                required_labels={"project": "P3"},
                allow_switching=True,
            ),
        ),
        frozenset(),
    )

    assert result is None


def test_allocator_uses_distinct_switch_ports() -> None:
    """不同设备走同一继电器的不同端口时可同时分配。"""

    allocator = ResourceAllocator()
    connection = SwitchConnection(
        switch_device_id="relay-board-1",
        ports=(
            SwitchPort(port="port1", labels={"project": "P1"}),
            SwitchPort(port="port3", labels={"project": "P3"}),
        ),
    )
    node = Node(
        id=None,
        node_id="n1",
        name="n1",
        hostname="n1",
        status=NodeStatus.ONLINE,
        online=True,
        enabled=True,
        devices=[
            _device(
                "can-a",
                capability=PhysicalDeviceCapability(
                    resource_type="can_channel", channel="can1",
                    connection=connection,
                ),
            ),
            _device(
                "can-b",
                capability=PhysicalDeviceCapability(
                    resource_type="can_channel", channel="can2",
                    connection=connection,
                ),
            ),
        ],
    )

    result = allocator.allocate(
        node,
        (
            DeviceRequirement(
                resource_type="can_channel",
                required_labels={"project": "P3"},
                allow_switching=True,
            ),
            DeviceRequirement(
                resource_type="can_channel",
                required_labels={"project": "P1"},
                allow_switching=True,
            ),
        ),
        frozenset(),
    )

    assert result is not None
    assert result.routes_by_device == {
        "can-a": SwitchRoute(switch_device_id="relay-board-1", port="port3"),
        "can-b": SwitchRoute(switch_device_id="relay-board-1", port="port1"),
    }
