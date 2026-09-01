"""M3 V2 Plan 物化集成测试。"""

from __future__ import annotations

from aetp_protocol.execution import LeaseState
from aetp_protocol.ids import BusinessId, stable_id
from aetp_protocol.plan_hash import with_plan_hash
from aetp_protocol.v2_envelope import parse_v2_message

from master.application.services.plan_lease_service import PlanLeaseService
from master.application.services.v2_plan_materialization_service import V2PlanMaterializationService
from master.domain.enums import NodeStatus, ProjectStatus, ShardAttemptStatus, ShardStatus
from master.domain.models import Node, NodeSession, Project, RunShard, TaskRun, TestScript, TestTask
from tests.test_m3_plan_lease import NOW, _plan

NODE_ID = BusinessId("01J00000000000000000000000")
SESSION_ID = "session-00000001"


def _seed_context(container, plan) -> None:
    assert container.auth_service().bootstrap_admin("materialize-admin", "admin-pass-123", "Materialize Admin")
    with container.uow_factory()() as uow:
        user = uow.users.get_by_username("materialize-admin")
        assert user is not None and user.id is not None
        uow.projects.add(
            Project(
                id=None,
                project_id=plan.project_id.root,
                project_key="MATERIALIZE",
                name="Materialize Project",
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
                name="materialize-script",
                version=plan.script.version,
                file_ref="scripts/materialize.zip",
                size=plan.script.size,
                sha256=plan.script.sha256.root,
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
                name="materialize-task",
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
                case_keys=list(plan.case_keys),
                status=ShardStatus.PENDING,
            )
        )
        uow.nodes.save(
            Node(
                id=None,
                node_id=plan.node_id.root,
                name="Bench 01",
                hostname="bench-01",
                status=NodeStatus.ONLINE,
                online=True,
                enabled=True,
            )
        )
        node = uow.nodes.get_by_id(plan.node_id.root)
        assert node is not None and node.id is not None
        uow.node_sessions.create(
            NodeSession(
                node_pk=node.id,
                node_id=plan.node_id.root,
                session_id=SESSION_ID,
                client_id="aetp-agent-materialize",
                connected_at=NOW,
            )
        )


def test_v2_materialization_is_atomic_and_idempotent(client) -> None:
    container = client.app.state.container
    plan = with_plan_hash(_plan())
    _seed_context(container, plan)
    plan_leases = PlanLeaseService(container.uow_factory(), now=lambda: NOW)
    service = V2PlanMaterializationService(container.uow_factory(), plan_leases)

    first = service.materialize(plan)
    repeated = service.materialize(plan)

    assert first.plan.id == repeated.plan.id
    assert first.attempt.id == repeated.attempt.id
    with container.uow_factory()() as uow:
        attempt = uow.shard_attempts.get_by_attempt_id(plan.attempt_id.root)
        shard = uow.run_shards.get_by_shard_id(plan.shard_id.root)
        run = uow.task_runs.get_by_run_id(plan.run_id.root)
        stored_plan = uow.execution_plans.get_by_plan_id(plan.plan_id)
        lease = uow.resource_leases.get_active_by_resource(plan.resource_bindings[0].resource_id)
        outbox = uow.outbox_messages.get_by_outbox_id(stable_id(f"execution-plan:{plan.plan_id.root}").root)
        assert attempt is not None and attempt.status is ShardAttemptStatus.DISPATCHED
        assert shard is not None and shard.status is ShardStatus.DISPATCHING
        assert run is not None and run.status.value == "dispatched"
        assert stored_plan is not None and stored_plan.plan == plan
        assert lease is not None and lease.lease.state is LeaseState.ACTIVE
        assert outbox is not None
        _, payload = parse_v2_message(outbox.payload)
        assert payload == plan


def test_v2_materialization_rejects_second_plan_for_same_attempt(client) -> None:
    container = client.app.state.container
    plan = with_plan_hash(_plan())
    _seed_context(container, plan)
    plan_leases = PlanLeaseService(container.uow_factory(), now=lambda: NOW)
    service = V2PlanMaterializationService(container.uow_factory(), plan_leases)
    service.materialize(plan)

    conflicting = with_plan_hash(
        plan.model_copy(
            update={
                "plan_id": BusinessId("01J00000000000000000000012"),
                "resource_bindings": (),
            }
        )
    )

    try:
        service.materialize(conflicting)
    except ValueError as exc:
        assert "Plan" in str(exc) or "plan" in str(exc)
    else:
        raise AssertionError("同一 Attempt 不应接受第二个 V2 Plan")
