from __future__ import annotations

from datetime import datetime, timezone

from aetp_protocol.capabilities import HardwareRequirements, NodeCapabilities

from master.domain.enums import DeviceStatus, NodeStatus, ShardAttemptStatus, ShardStatus
from master.domain.models import Device, Node, RunShard, ShardAttempt
from master.domain.scheduler import NodeSchedulingState, ShardScheduler


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
) -> Device:
    return Device(
        id=None,
        device_id=device_id,
        node_id=None,
        name=device_id,
        status=status,
        online=online,
    )


def _shard() -> RunShard:
    return RunShard(
        shard_id="shard-1",
        run_id="run-1",
        shard_index=0,
        status=ShardStatus.PENDING,
    )


def test_scheduler_waits_when_device_is_occupied() -> None:
    scheduler = ShardScheduler()
    shard = _shard()
    candidates = [
        _node("device-busy", devices=(_device("device-a", status=DeviceStatus.BUSY),)),
        _node("available", devices=(_device("device-b"),)),
    ]

    selected = scheduler.select_node(
        shard=shard,
        requirements=HardwareRequirements(),
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
        shard=_shard(),
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
        shard=_shard(),
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
        shard=_shard(),
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
