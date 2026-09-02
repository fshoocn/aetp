"""V2 多脚本 Scheduler 测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from aetp_protocol.capabilities import ExecutorCapability, NodeCapabilitySnapshot, ResourceCapability, ResourceHealth
from aetp_protocol.execution import ExecutionRequirement, PluginRequirement, ResourceRequirement, SplitPolicy
from aetp_protocol.ids import BusinessId, PluginId, SemVer, SessionId, Sha256, VersionRange
from aetp_protocol.plugin_types import PluginAvailability, PluginPoint
from aetp_protocol.task import TestTask as ProtocolTestTask

from master.application.services.v2_scheduler_service import V2SchedulerService
from master.domain.enums import AccountStatus, NodeStatus, PlatformRole, ProjectStatus
from master.domain.models import Node, NodeCapabilitySnapshotRecord, Project, User
from master.domain.time import utcnow
from tests.test_v2_task_service import (
    PROJECT_ID,
    SCRIPT_A,
    SCRIPT_B,
    TASK_ID,
    _binding,
    _definition,
)

NODE_ID = BusinessId("01J00000000000000000000060")
SESSION_ID = SessionId("session-00000060")


def _seed(container) -> None:
    now = utcnow()
    with container.uow_factory()() as uow:
        user = uow.users.add(
            User(
                id=None,
                username="v2-scheduler-owner",
                password_hash="hash",
                display_name="V2 Scheduler Owner",
                account_status=AccountStatus.ACTIVE,
                platform_role=PlatformRole.ADMIN,
                created_at=now,
                updated_at=now,
            )
        )
        uow.projects.add(
            Project(
                id=None,
                project_id=PROJECT_ID.root,
                project_key="V2SCHED",
                name="V2 Scheduler",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
        )
        node = uow.nodes.save(
            Node(
                id=None,
                node_id=NODE_ID.root,
                name="V2 Bench",
                hostname="v2-bench",
                status=NodeStatus.ONLINE,
                online=True,
                enabled=True,
            )
        )
        assert node.id is not None
        uow.node_sessions.create(
            __import__("master.domain.models", fromlist=["NodeSession"]).NodeSession(
                node_pk=node.id,
                node_id=NODE_ID.root,
                session_id=SESSION_ID.root,
                client_id="v2-agent",
                connected_at=now,
            )
        )
        snapshot = NodeCapabilitySnapshot(
            schema_version=2,
            node_id=NODE_ID,
            session_id=SESSION_ID,
            revision=1,
            reported_at=datetime.now(UTC),
            maintenance_state="idle",
            executors=(
                ExecutorCapability(
                    plugin_id=PluginId("org.pytest.executor"),
                    version=SemVer("2.0.0"),
                    capabilities=(),
                ),
            ),
            plugin_inventory=(
                {
                    "plugin_id": "org.pytest.executor",
                    "point": PluginPoint.EXECUTOR.value,
                    "version": "2.0.0",
                    "archive_sha256": "a" * 64,
                    "availability": PluginAvailability.AVAILABLE.value,
                    "checked_at": datetime.now(UTC).isoformat(),
                },
            ),
        )
        uow.node_capability_snapshots.add_if_newer(
            NodeCapabilitySnapshotRecord(
                id=None,
                node_id=NODE_ID,
                session_id=SESSION_ID,
                revision=1,
                snapshot_sha256=Sha256("d" * 64),
                snapshot=snapshot,
                reported_at=snapshot.reported_at,
                created_at=now,
            )
        )


def _create_task(container, *, execution_mode: str, stop_on_failure: bool):
    service = container.v2_task_service()
    definition_a = service.register_script_definition(_definition(SCRIPT_A, name="alpha"))
    definition_b = service.register_script_definition(_definition(SCRIPT_B, name="beta"))
    task = ProtocolTestTask(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        revision=1,
        name="scheduler task",
        scripts=(
            _binding("01J00000000000000000000061", definition_a.definition, 0),
            _binding("01J00000000000000000000062", definition_b.definition, 1),
        ),
        execution_mode=execution_mode,
        stop_on_failure=stop_on_failure,
        node_ids=(NODE_ID,),
    )
    service.create_task(task, created_by=definition_a.id or 1)
    return service.create_run(TASK_ID, project_id=PROJECT_ID)


def test_v2_scheduler_materializes_plans_per_script_binding(client) -> None:
    container = client.app.state.container
    _seed(container)
    created = _create_task(container, execution_mode="parallel", stop_on_failure=False)
    scheduler: V2SchedulerService = container.v2_scheduler_service()

    result = scheduler.schedule_run(BusinessId(created.run.run_id))

    assert len(result.scheduled) == 4
    assert not result.pending_shard_ids
    assert {item.plan.script_binding_id.root for item in result.scheduled} == {
        "01J00000000000000000000061",
        "01J00000000000000000000062",
    }
    assert all(item.plan.target_session_id == SESSION_ID for item in result.scheduled)
    with container.uow_factory()() as uow:
        plans = uow.execution_plans.list_by_run(BusinessId(created.run.run_id))
        assert len(plans) == 4
        assert all(plan.plan.script.download_url for plan in plans)


def test_v2_scheduler_sequence_stops_later_scripts_after_failure(client) -> None:
    container = client.app.state.container
    _seed(container)
    created = _create_task(container, execution_mode="sequence", stop_on_failure=True)
    scheduler: V2SchedulerService = container.v2_scheduler_service()

    first = scheduler.schedule_run(BusinessId(created.run.run_id))
    assert len(first.scheduled) == 2
    first_shards = first.scheduled
    with container.uow_factory()() as uow:
        for materialized in first_shards:
            shard = uow.run_shards.get_by_shard_id(materialized.plan.shard_id.root)
            attempt = uow.shard_attempts.get_by_attempt_id(materialized.plan.attempt_id.root)
            assert shard is not None and attempt is not None
            shard.status = __import__("master.domain.enums", fromlist=["ShardStatus"]).ShardStatus.FAILED
            attempt.status = __import__(
                "master.domain.enums",
                fromlist=["ShardAttemptStatus"],
            ).ShardAttemptStatus.FAILED
            uow.run_shards.update(shard)
            uow.shard_attempts.update(attempt)

    second = scheduler.schedule_run(BusinessId(created.run.run_id))

    assert not second.scheduled
    assert len(second.cancelled_shard_ids) == 2
    with container.uow_factory()() as uow:
        later = [
            shard
            for shard in uow.run_shards.list_by_run(created.run.run_id)
            if shard.script_binding_id == "01J00000000000000000000062"
        ]
        assert later and all(shard.status.value == "cancelled" for shard in later)


def test_v2_scheduler_materializes_concrete_resource_lease(client) -> None:
    container = client.app.state.container
    _seed(container)
    service = container.v2_task_service()
    definition = service.register_script_definition(
        _definition(SCRIPT_A, name="resource-alpha").model_copy(
            update={
                "requirement": ExecutionRequirement(
                    executor=PluginRequirement(
                        plugin_id=PluginId("org.pytest.executor"),
                        version=VersionRange(exact=SemVer("2.0.0")),
                    ),
                    resources=(ResourceRequirement(resource_type="can", quantity=1),),
                )
            }
        )
    )
    binding = _binding("01J00000000000000000000064", definition.definition, 0).model_copy(
        update={"split_policy": SplitPolicy(type="none")}
    )
    task = ProtocolTestTask(
        task_id=BusinessId("01J00000000000000000000063"),
        project_id=PROJECT_ID,
        revision=1,
        name="resource task",
        scripts=(binding,),
        node_ids=(NODE_ID,),
    )
    service.create_task(task, created_by=definition.id or 1)
    created = service.create_run(task.task_id, project_id=PROJECT_ID)
    with container.uow_factory()() as uow:
        latest = uow.node_capability_snapshots.get_latest(NODE_ID)
        assert latest is not None
        snapshot = latest.snapshot.model_copy(
            update={
                "revision": latest.revision + 1,
                "reported_at": datetime.now(UTC),
                "resources": (
                    ResourceCapability(
                        resource_id=BusinessId("01J00000000000000000000065"),
                        provider_id="vector",
                        resource_type="can",
                        health=ResourceHealth.READY,
                    ),
                ),
            }
        )
        uow.node_capability_snapshots.add_if_newer(
            NodeCapabilitySnapshotRecord(
                id=None,
                node_id=NODE_ID,
                session_id=SESSION_ID,
                revision=snapshot.revision,
                snapshot_sha256=Sha256("e" * 64),
                snapshot=snapshot,
                reported_at=snapshot.reported_at,
                created_at=datetime.now(UTC),
            )
        )

    result = container.v2_scheduler_service().schedule_run(BusinessId(created.run.run_id))

    assert len(result.scheduled) == 1
    assert all(item.plan.resource_bindings for item in result.scheduled)
    with container.uow_factory()() as uow:
        leases = [
            uow.resource_leases.get_active_by_resource(item.plan.resource_bindings[0].resource_id)
            for item in result.scheduled
        ]
        assert sum(lease is not None for lease in leases) == 1
