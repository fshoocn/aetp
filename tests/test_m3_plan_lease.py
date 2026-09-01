"""M3 V2 ExecutionPlan 和 ResourceLease 测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aetp_protocol.artifacts import Configuration, ScriptRef
from aetp_protocol.execution import (
    ExecutionPlan,
    ExecutorRef,
    LeaseState,
    PlanResourceBinding,
)
from aetp_protocol.ids import BusinessId, PluginId, SemVer, SessionId, Sha256, stable_id
from aetp_protocol.message_types import MessageType
from aetp_protocol.payloads import LeaseRenewRequest
from aetp_protocol.plugin_types import PluginDistributionRef
from aetp_protocol.topics import v2_command_topic
from aetp_protocol.v2_envelope import parse_v2_message

from master.application.services.plan_lease_service import (
    PlanLeaseService,
    ResourceLeaseConflict,
    calculate_plan_hash,
    with_plan_hash,
)
from master.domain.enums import (
    NodeStatus,
    ProjectStatus,
    ScriptParseLocation,
    ScriptParseStatus,
    ShardAttemptStatus,
    ShardStatus,
)
from master.domain.models import Node, NodeSession, Project, RunShard, ShardAttempt, TaskRun, TestScript, TestTask

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = SessionId("session-00000001")
RUN_ID = BusinessId("01J00000000000000000000001")
TASK_ID = BusinessId("01J00000000000000000000002")
SCRIPT_BINDING_ID = BusinessId("01J00000000000000000000003")
SCRIPT_DEFINITION_ID = BusinessId("01J00000000000000000000004")
SHARD_ID = BusinessId("01J00000000000000000000005")
ATTEMPT_ID = BusinessId("01J00000000000000000000006")
PLAN_ID = BusinessId("01J00000000000000000000007")
RESOURCE_1 = BusinessId("01J00000000000000000000008")
RESOURCE_2 = BusinessId("01J00000000000000000000009")
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _seed_node(container) -> None:
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
                client_id="aetp-agent-bench-01",
                connected_at=NOW,
            )
        )


def _plan(
    *,
    plan_id: BusinessId = PLAN_ID,
    resource_ids: tuple[BusinessId, ...] = (RESOURCE_1,),
    run_id: BusinessId = RUN_ID,
    attempt_id: BusinessId = ATTEMPT_ID,
    attempt_no: int = 1,
    script_download_url: str | None = "https://master/script.zip?signature=one",
    artifact_upload_url: str | None = "https://master/artifacts?signature=one",
) -> ExecutionPlan:
    bindings = tuple(
        PlanResourceBinding(
            lease_id=stable_id(f"{plan_id.root}:lease:{resource_id.root}"),
            resource_id=resource_id,
            resource_type="can",
            properties={"can_fd": True},
            labels={"bus": resource_id.root},
            lease_revision=1,
            expires_at=NOW + timedelta(minutes=5),
        )
        for resource_id in resource_ids
    )
    return ExecutionPlan(
        schema_version=2,
        plan_id=plan_id,
        plan_hash=Sha256("0" * 64),
        run_id=run_id,
        task_id=TASK_ID,
        script_binding_id=SCRIPT_BINDING_ID,
        script_definition_id=SCRIPT_DEFINITION_ID,
        shard_id=SHARD_ID,
        attempt_id=attempt_id,
        attempt_no=attempt_no,
        project_id=BusinessId("01J00000000000000000000010"),
        node_id=NODE_ID,
        target_session_id=SESSION_ID,
        executor=ExecutorRef(
            plugin_id=PluginId("org.pytest.executor"),
            version=SemVer("2.0.0"),
        ),
        plugin_package=PluginDistributionRef(
            plugin_id=PluginId("org.pytest.executor"),
            version=SemVer("2.0.0"),
            archive_sha256=Sha256("a" * 64),
            download_url="https://master/plugin.zip?signature=one",
        ),
        resource_bindings=bindings,
        script=ScriptRef(
            script_id=BusinessId("01J00000000000000000000011"),
            version=1,
            filename="tests.zip",
            size=1024,
            sha256=Sha256("b" * 64),
            download_url=script_download_url,
        ),
        configuration=Configuration(
            schema_version=1,
            schema_hash=Sha256("c" * 64),
            values={"markers": ["smoke"]},
        ),
        execution_parameters={"mode": "quick"},
        case_keys=("tests/test_login.py::test_login",),
        artifact_upload_url=artifact_upload_url,
        created_at=NOW,
        deadline_at=NOW + timedelta(hours=1),
    )


def test_plan_hash_excludes_temporary_urls_and_includes_semantics() -> None:
    first = with_plan_hash(_plan())
    second = with_plan_hash(
        _plan(
            script_download_url="https://other/script.zip?signature=two",
            artifact_upload_url="https://other/artifacts?signature=two",
        )
    )
    changed = with_plan_hash(_plan().model_copy(update={"case_keys": ("tests/test_other.py::test_other",)}))

    assert first.plan_hash == second.plan_hash
    assert first.plan_hash != changed.plan_hash
    assert calculate_plan_hash(first) == first.plan_hash


def test_plan_allocation_is_idempotent_and_publishes_v2_command(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    service = PlanLeaseService(container.uow_factory(), now=lambda: NOW)
    plan = with_plan_hash(_plan())

    first = service.allocate(plan)
    repeated = service.allocate(plan)

    assert repeated.id == first.id
    with container.uow_factory()() as uow:
        stored = uow.execution_plans.get_by_plan_id(plan.plan_id)
        assert stored is not None
        lease = uow.resource_leases.get_active_by_resource(RESOURCE_1)
        assert lease is not None
        assert lease.lease.state is LeaseState.ACTIVE
        outbox = uow.outbox_messages.get_by_outbox_id(stable_id(f"execution-plan:{plan.plan_id.root}").root)
        assert outbox is not None
        envelope, payload = parse_v2_message(outbox.payload)
        assert envelope.message_type == MessageType.EXECUTION_PLAN.value
        assert payload == plan
        assert outbox.topic == v2_command_topic(NODE_ID.root, "execution.plan")


def test_plan_resource_conflict_rolls_back_all_new_leases(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    service = PlanLeaseService(container.uow_factory(), now=lambda: NOW)
    service.allocate(with_plan_hash(_plan(resource_ids=(RESOURCE_2,))))
    conflicting = with_plan_hash(
        _plan(
            plan_id=BusinessId("01J00000000000000000000012"),
            attempt_id=BusinessId("01J00000000000000000000013"),
            resource_ids=(RESOURCE_1, RESOURCE_2),
        )
    )

    with pytest.raises(ResourceLeaseConflict):
        service.allocate(conflicting)

    with container.uow_factory()() as uow:
        assert uow.resource_leases.get_active_by_resource(RESOURCE_1) is None
        assert uow.resource_leases.get_active_by_resource(RESOURCE_2) is not None
        assert uow.execution_plans.get_by_plan_id(conflicting.plan_id) is None


def test_lease_renew_requires_current_session_revision_and_deadline(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    service = PlanLeaseService(container.uow_factory(), now=lambda: NOW)
    plan = with_plan_hash(_plan())
    service.allocate(plan)
    binding = plan.resource_bindings[0]
    request = LeaseRenewRequest(
        plan_id=plan.plan_id,
        attempt_id=plan.attempt_id,
        lease_id=binding.lease_id,
        revision=1,
        requested_expires_at=NOW + timedelta(minutes=4),
    )

    renewed = service.renew(request, node_id=NODE_ID, session_id=SESSION_ID)
    stale = service.renew(request, node_id=NODE_ID, session_id=SESSION_ID)
    wrong_session = service.renew(request, node_id=NODE_ID, session_id=SessionId("session-00000002"))

    assert renewed.accepted is True
    assert renewed.revision == 2
    assert renewed.expires_at == request.requested_expires_at
    assert stale.accepted is False
    assert stale.code is not None and stale.code.root == "RESOURCE_LEASE_EXPIRED"
    assert wrong_session.accepted is False
    assert wrong_session.code is not None and wrong_session.code.root == "STALE_SESSION"


def test_release_and_expire_are_conditional_and_idempotent(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    clock = [NOW]
    service = PlanLeaseService(container.uow_factory(), now=lambda: clock[0])
    first = with_plan_hash(_plan())
    service.allocate(first)
    released = service.release_lease(first.resource_bindings[0].lease_id, expected_revision=1)
    repeated = service.release_lease(first.resource_bindings[0].lease_id, expected_revision=1)
    assert released is not None
    assert released.lease.state is LeaseState.RELEASED
    assert repeated is None

    second = with_plan_hash(
        _plan(
            plan_id=BusinessId("01J00000000000000000000014"),
            attempt_id=BusinessId("01J00000000000000000000015"),
            attempt_no=2,
        )
    )
    service.allocate(second)
    clock[0] = NOW + timedelta(minutes=6)
    expired = service.expire_due()
    assert len(expired) == 1
    assert expired[0].lease.state is LeaseState.EXPIRED
    assert service.expire_due() == ()


def test_expired_lease_moves_attempt_to_unknown_and_shard_to_recovery(client) -> None:
    container = client.app.state.container
    _seed_node(container)
    assert container.auth_service().bootstrap_admin("lease-admin", "admin-pass-123", "Lease Admin")
    clock = [NOW]
    service = PlanLeaseService(container.uow_factory(), now=lambda: clock[0])
    plan = with_plan_hash(_plan())
    with container.uow_factory()() as uow:
        user = uow.users.get_by_username("lease-admin")
        assert user is not None and user.id is not None
        uow.projects.add(
            Project(
                id=None,
                project_id=plan.project_id.root,
                project_key="LEASE",
                name="Lease Project",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        uow.test_scripts.add(
            TestScript(
                project_id=plan.project_id.root,
                script_id=plan.script.script_id.root,
                task_type="example",
                name="lease-script",
                version=plan.script.version,
                file_ref="scripts/lease.zip",
                size=plan.script.size,
                sha256=plan.script.sha256.root,
                parse_status=ScriptParseStatus.PARSED,
                parse_location=ScriptParseLocation.MASTER,
                result_parse_location=ScriptParseLocation.MASTER,
                plugin_version="2.0.0",
                created_by=user.id,
            )
        )
        uow.test_tasks.add(
            TestTask(
                task_id=plan.task_id.root,
                project_id=plan.project_id.root,
                script_id=plan.script.script_id.root,
                script_version=plan.script.version,
                task_type="example",
                name="lease-task",
                created_by=user.id,
            )
        )
        uow.task_runs.add(
            TaskRun(
                run_id=plan.run_id.root,
                project_id=plan.project_id.root,
                task_id=plan.task_id.root,
            )
        )
        uow.run_shards.add(
            RunShard(
                shard_id=plan.shard_id.root,
                run_id=plan.run_id.root,
                shard_index=0,
                status=ShardStatus.DISPATCHING,
            )
        )
        uow.shard_attempts.add(
            ShardAttempt(
                attempt_id=plan.attempt_id.root,
                shard_id=plan.shard_id.root,
                attempt_no=plan.attempt_no,
                node_id=plan.node_id.root,
                status=ShardAttemptStatus.RUNNING,
            )
        )
    service.allocate(plan)
    clock[0] = NOW + timedelta(minutes=6)

    expired = service.expire_due()

    assert len(expired) == 1
    with container.uow_factory()() as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(plan.attempt_id.root)
        shard = uow.run_shards.get_by_shard_id(plan.shard_id.root)
        run = uow.task_runs.get_by_run_id(plan.run_id.root)
        assert attempt is not None and attempt.status is ShardAttemptStatus.UNKNOWN
        assert attempt.error_code == "RESOURCE_LEASE_EXPIRED"
        assert shard is not None and shard.status is ShardStatus.WAITING_RECOVERY
        assert run is not None and run.status.value == "created"
