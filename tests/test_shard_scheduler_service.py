from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aetp_protocol.capabilities import (
    DeviceRequirement,
    HardwareRequirements,
    NodeCapabilities,
    NumericConstraint,
    PhysicalDeviceCapability,
    SystemRequirement,
)
from aetp_protocol.envelope import Envelope
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import RunAssignPayload

from master.application.errors import (
    NodeCapabilityMismatchError,
    ProjectAccessDeniedError,
)
from master.domain.enums import (
    AccountStatus,
    DeviceStatus,
    NodeStatus,
    PlatformRole,
    ProjectStatus,
    RunStatus,
    ScriptParseLocation,
    ScriptParseStatus,
    ShardAttemptStatus,
    ShardStatus,
    TriggerType,
)
from master.domain.models import (
    Device,
    Node,
    Project,
    ProjectNodeBinding,
    RunShard,
    TaskRun,
    TestScript,
    TestTask,
    User,
)
from master.domain.time import utcnow


def _uow(container):
    return container.uow_factory()()


def _seed(
    container,
    *,
    node_ids: tuple[str, ...] = ("node-a", "node-b"),
    task_node_ids: list[str] | None = None,
    busy_node_ids: set[str] | None = None,
    retry_policy: dict | None = None,
    requirements: HardwareRequirements | None = None,
    device_specs: dict[str, tuple[tuple[str, PhysicalDeviceCapability], ...]] | None = None,
    shard_count: int = 2,
) -> str:
    task_node_ids = task_node_ids if task_node_ids is not None else list(node_ids)
    busy_node_ids = busy_node_ids or set()
    device_specs = device_specs or {}
    with _uow(container) as uow:
        user = uow.users.add(
            User(
                id=None,
                username="scheduler-owner",
                password_hash="hash",
                display_name="Scheduler Owner",
                account_status=AccountStatus.ACTIVE,
                platform_role=PlatformRole.USER,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        uow.projects.add(
            Project(
                id=None,
                project_id="project-scheduler",
                project_key="SCHEDULER",
                name="Scheduler",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        script = uow.test_scripts.add(
            TestScript(
                project_id="project-scheduler",
                script_id="script-scheduler",
                task_type="pytest",
                name="scheduler-script",
                version=1,
                file_ref="data/scripts/script-scheduler/1",
                size=1,
                sha256="a" * 64,
                hardware_requirements=requirements or HardwareRequirements(),
                parse_status=ScriptParseStatus.PARSED,
                parse_location=ScriptParseLocation.MASTER,
                result_parse_location=ScriptParseLocation.MASTER,
                plugin_version="1.0.0",
                created_by=user.id,
            )
        )
        for index, node_id in enumerate(node_ids):
            node = uow.nodes.save(
                Node(
                    id=None,
                    node_id=node_id,
                    name=node_id,
                    hostname=node_id,
                    status=NodeStatus.ONLINE,
                    online=True,
                    enabled=True,
                    capabilities=NodeCapabilities(
                    ),
                    devices=[],
                    load={"queued_shards": index},
                    last_seen_at=datetime(
                        2026, 1, 1, 0, 0, index, tzinfo=UTC
                    ),
                )
            )
            assert node.id is not None
            specs = device_specs.get(
                node_id,
                ((f"{node_id}-device", PhysicalDeviceCapability(resource_type="generic")),),
            )
            for device_id, capability in specs:
                uow.devices.add(
                    Device(
                        id=None,
                        device_id=device_id,
                        node_id=node_id,
                        name=device_id,
                        capability=capability,
                        status=(
                            DeviceStatus.BUSY
                            if node_id in busy_node_ids
                            else DeviceStatus.ONLINE
                        ),
                        online=True,
                    )
                )
            uow.bindings.add(
                ProjectNodeBinding(
                    id=None,
                    project_id="project-scheduler",
                    node_id=node_id,
                    enabled=True,
                    assigned_by=user.id,
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
        task = uow.test_tasks.add(
            TestTask(
                project_id="project-scheduler",
                task_id="task-scheduler",
                script_id=script.script_id,
                script_version=1,
                task_type="pytest",
                name="scheduler-task",
                default_case_selection=[f"case-{i}" for i in range(shard_count)],
                node_ids=task_node_ids,
                split_policy={"type": "none"},
                retry_policy=retry_policy or {},
                timeout_s=120,
                enabled=True,
                created_by=user.id,
            )
        )
        run = uow.task_runs.add(
            TaskRun(
                run_id="run-scheduler",
                project_id=task.project_id,
                task_id=task.task_id,
                script_ref={
                    "script_id": script.script_id,
                    "version": script.version,
                    "sha256": script.sha256,
                },
                case_selection=task.default_case_selection,
                split_policy=task.split_policy,
                trigger_type=TriggerType.MANUAL_WEB,
                triggered_by_user_id=user.id,
                status=RunStatus.CREATED,
            )
        )
        uow.run_shards.add_many(
            [
                RunShard(
                    shard_id=f"shard-{i}",
                    run_id=run.run_id,
                    shard_index=i,
                    case_keys=[f"case-{i}"],
                    execution_params={"channel": i},
                    status=ShardStatus.PENDING,
                )
                for i in range(shard_count)
            ]
        )
    return "run-scheduler"


def test_schedule_run_persists_attempt_and_run_assign_outbox(client) -> None:
    container = client.app.state.container
    run_id = _seed(container, shard_count=1)

    result = container.shard_scheduler_service().schedule_run(run_id)

    assert len(result.scheduled) == 1
    assert result.pending_shard_ids == ()
    dispatch = result.scheduled[0]
    with _uow(container) as uow:
        attempts = uow.shard_attempts.list_by_shard(dispatch.shard_id)
        assert [(a.attempt_no, a.node_id, a.status) for a in attempts] == [
            (1, "node-b", ShardAttemptStatus.DISPATCHED)
        ]
        message = uow.outbox_messages.get_by_outbox_id(dispatch.outbox_id)
        assert message is not None
        assert message.topic.endswith("/node-b/commands/assign")
        envelope = Envelope.model_validate(message.payload)
        assert envelope.message_type == MessageType.RUN_ASSIGN.value
        payload = RunAssignPayload.model_validate(envelope.payload)
        assert payload.run_id == run_id
        assert payload.shard_id == dispatch.shard_id
        assert payload.execution_params == {"channel": 0}
        assert payload.device_allocations == []
        run = uow.task_runs.get_by_run_id(run_id)
        assert run is not None
        assert run.status is RunStatus.DISPATCHED


def test_schedule_run_is_idempotent(client) -> None:
    container = client.app.state.container
    run_id = _seed(container, shard_count=2)
    service = container.shard_scheduler_service()

    first = service.schedule_run(run_id)
    second = service.schedule_run(run_id)

    assert len(first.scheduled) == 2
    assert first.pending_shard_ids == ()
    assert second.scheduled == ()
def test_schedule_run_keeps_shard_pending_when_all_devices_are_busy(client) -> None:
    container = client.app.state.container
    run_id = _seed(
        container,
        node_ids=("node-a",),
        busy_node_ids={"node-a"},
        requirements=HardwareRequirements(
            devices=(DeviceRequirement(resource_type="generic"),)
        ),
        shard_count=1,
    )

    result = container.shard_scheduler_service().schedule_run(run_id)

    assert result.scheduled == ()
    assert result.pending_shard_ids == ("shard-0",)
    with _uow(container) as uow:
        assert uow.shard_attempts.list_by_shard("shard-0") == []
        run = uow.task_runs.get_by_run_id(run_id)
        assert run is not None
        assert run.status is RunStatus.CREATED


def test_schedule_run_reserves_each_free_device_once_per_round(client) -> None:
    container = client.app.state.container
    run_id = _seed(
        container,
        node_ids=("node-a",),
        requirements=HardwareRequirements(
            devices=(DeviceRequirement(resource_type="generic"),)
        ),
        shard_count=2,
    )

    result = container.shard_scheduler_service().schedule_run(run_id)

    assert len(result.scheduled) == 1
    assert result.pending_shard_ids == ("shard-1",)


def test_schedule_run_allocates_multiple_and_specific_devices_atomically(client) -> None:
    container = client.app.state.container
    requirements = HardwareRequirements(
        devices=(
            DeviceRequirement(
                resource_type="can_channel",
                vendor="vector",
                model="1640",
                quantity=2,
                device_ids=("can1",),
            ),
            DeviceRequirement(
                resource_type="eth_channel",
                vendor="vector",
                model="1640",
                channel="eth1",
            ),
            DeviceRequirement(
                resource_type="relay_board",
                device_ids=("relay-board-2",),
            ),
            DeviceRequirement(
                resource_type="power_supply",
                device_ids=("power-supply-1",),
            ),
        )
    )
    device_specs = {
        "node-a": (
            (
                "can1",
                PhysicalDeviceCapability(
                    resource_type="can_channel",
                    vendor="vector",
                    model="1640",
                    channel="can1",
                ),
            ),
            (
                "can2",
                PhysicalDeviceCapability(
                    resource_type="can_channel",
                    vendor="vector",
                    model="1640",
                    channel="can2",
                ),
            ),
            (
                "eth1",
                PhysicalDeviceCapability(
                    resource_type="eth_channel",
                    vendor="vector",
                    model="1640",
                    channel="eth1",
                ),
            ),
            (
                "relay-board-2",
                PhysicalDeviceCapability(resource_type="relay_board"),
            ),
            (
                "power-supply-1",
                PhysicalDeviceCapability(resource_type="power_supply"),
            ),
        )
    }
    run_id = _seed(
        container,
        node_ids=("node-a",),
        requirements=requirements,
        device_specs=device_specs,
        shard_count=1,
    )

    result = container.shard_scheduler_service().schedule_run(run_id)

    assert len(result.scheduled) == 1
    dispatch = result.scheduled[0]
    expected_ids = {
        "can1",
        "can2",
        "eth1",
        "relay-board-2",
        "power-supply-1",
    }
    assert set(dispatch.device_ids) == expected_ids
    with _uow(container) as uow:
        attempt = uow.shard_attempts.list_by_shard(dispatch.shard_id)[0]
        assert set(attempt.device_ids) == expected_ids
        message = uow.outbox_messages.get_by_outbox_id(dispatch.outbox_id)
        assert message is not None
        payload = RunAssignPayload.model_validate(
            Envelope.model_validate(message.payload).payload
        )
        assert {allocation.device_id for allocation in payload.device_allocations} == expected_ids


def test_schedule_run_reports_specific_device_capability_mismatch(client) -> None:
    container = client.app.state.container
    run_id = _seed(
        container,
        node_ids=("node-a",),
        requirements=HardwareRequirements(
            devices=(
                DeviceRequirement(
                    resource_type="relay_board",
                    device_ids=("relay-board-2",),
                ),
            )
        ),
        device_specs={
            "node-a": (
                (
                    "relay-board-1",
                    PhysicalDeviceCapability(resource_type="relay_board"),
                ),
            )
        },
        shard_count=1,
    )

    with pytest.raises(NodeCapabilityMismatchError, match="物理设备集合"):
        container.shard_scheduler_service().schedule_run(run_id)


def test_schedule_run_rejects_node_outside_project_binding(client) -> None:
    container = client.app.state.container
    run_id = _seed(container, node_ids=("node-a",), task_node_ids=["node-not-bound"])

    with pytest.raises(ProjectAccessDeniedError):
        container.shard_scheduler_service().schedule_run(run_id)


def test_schedule_run_rejects_when_online_nodes_lack_capability(client) -> None:
    container = client.app.state.container
    requirements = HardwareRequirements(
        system=SystemRequirement(
            memory_mb=NumericConstraint(minimum=999999),
        )
    )
    run_id = _seed(
        container,
        node_ids=("node-a",),
        requirements=requirements,
        shard_count=1,
    )

    with pytest.raises(NodeCapabilityMismatchError) as error:
        container.shard_scheduler_service().schedule_run(run_id)

    assert error.value.code == "NODE_CAPABILITY_MISMATCH"


def test_schedule_run_failover_uses_new_node_and_preserves_history(client) -> None:
    container = client.app.state.container
    run_id = _seed(
        container,
        node_ids=("node-a", "node-b"),
        shard_count=1,
        retry_policy={"max_attempts": 2, "failover_nodes": True},
    )
    service = container.shard_scheduler_service()
    first = service.schedule_run(run_id)

    with _uow(container) as uow:
        attempt = uow.shard_attempts.list_by_shard(first.scheduled[0].shard_id)[0]
        attempt.status = ShardAttemptStatus.FAILED
        attempt.error_code = "DISPATCH_TIMEOUT"
        attempt.error_message = "ACK timeout"
        uow.shard_attempts.update(attempt)

    second = service.schedule_run(run_id)

    assert len(second.scheduled) == 1
    with _uow(container) as uow:
        attempts = uow.shard_attempts.list_by_shard(first.scheduled[0].shard_id)
        assert [(a.attempt_no, a.node_id, a.status) for a in attempts] == [
            (1, "node-b", ShardAttemptStatus.FAILED),
            (2, "node-a", ShardAttemptStatus.DISPATCHED),
        ]
