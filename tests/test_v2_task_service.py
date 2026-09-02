"""V2 TaskService 多脚本快照与 Shard 展开测试。"""

from __future__ import annotations

from aetp_protocol.artifacts import CaseSelection, Configuration, ScriptRef
from aetp_protocol.artifacts import TestCase as ProtocolTestCase
from aetp_protocol.execution import SplitPolicy
from aetp_protocol.ids import BusinessId, PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import PluginRef
from aetp_protocol.task import ScriptDefinition, TaskScriptRef
from aetp_protocol.task import TestTask as ProtocolTestTask

from master.domain.enums import AccountStatus, PlatformRole, ProjectStatus
from master.domain.models import Project, User
from master.domain.time import utcnow

PROJECT_ID = BusinessId("01J00000000000000000000040")
SCRIPT_A = BusinessId("01J00000000000000000000041")
SCRIPT_B = BusinessId("01J00000000000000000000042")
TASK_ID = BusinessId("01J00000000000000000000043")
RUN_ID = BusinessId("01J00000000000000000000044")
CONFIG_HASH = Sha256("c" * 64)


def _definition(script_id: BusinessId, *, name: str) -> ScriptDefinition:
    return ScriptDefinition(
        script_definition_id=script_id,
        project_id=PROJECT_ID,
        revision=1,
        name=name,
        executor=PluginRef(
            plugin_id=PluginId("org.pytest.executor"),
            version=SemVer("2.0.0"),
            archive_sha256=Sha256("a" * 64),
        ),
        source=ScriptRef(
            script_id=script_id,
            version=1,
            filename=f"{name}.zip",
            size=20,
            sha256=Sha256("b" * 64),
            download_url="https://temporary.invalid/source.zip",
        ),
        configuration=Configuration(schema_version=1, schema_hash=CONFIG_HASH, values={}),
        cases=(
            ProtocolTestCase(stable_key=f"{name}-a", name="A", estimated_duration_s=2),
            ProtocolTestCase(stable_key=f"{name}-b", name="B", estimated_duration_s=3),
        ),
    )


def _binding(binding_id: str, definition: ScriptDefinition, order_index: int) -> TaskScriptRef:
    return TaskScriptRef(
        binding_id=BusinessId(binding_id),
        script_definition_id=definition.script_definition_id,
        script_revision=definition.revision,
        case_selection=CaseSelection(include_all=True),
        configuration=definition.configuration,
        split_policy=SplitPolicy(type="by_case_count", target_count=1),
        order_index=order_index,
    )


def _seed_project(container) -> None:
    now = utcnow()
    with container.uow_factory()() as uow:
        user = uow.users.add(
            User(
                id=None,
                username="v2-task-service-owner",
                password_hash="hash",
                display_name="V2 Task Service Owner",
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
                project_key="V2TSVC",
                name="V2 Task Service",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=now,
                updated_at=now,
            )
        )


def test_v2_task_service_creates_multiscript_snapshot_and_shards(client) -> None:
    container = client.app.state.container
    _seed_project(container)
    service = container.v2_task_service()
    definition_a = service.register_script_definition(_definition(SCRIPT_A, name="alpha"))
    definition_b = service.register_script_definition(_definition(SCRIPT_B, name="beta"))
    task = ProtocolTestTask(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        revision=1,
        name="two scripts",
        scripts=(
            _binding("01J00000000000000000000045", definition_a.definition, 0),
            _binding("01J00000000000000000000046", definition_b.definition, 1),
        ),
        execution_mode="sequence",
        stop_on_failure=True,
    )
    stored_task = service.create_task(task, created_by=definition_a.id or 1)
    created = service.create_run(TASK_ID, project_id=PROJECT_ID, run_id=RUN_ID)
    repeated = service.create_run(TASK_ID, project_id=PROJECT_ID, run_id=RUN_ID)

    assert stored_task.task == task
    assert created.run.run_id == RUN_ID.root
    assert repeated.run.id == created.run.id
    assert created.snapshot.execution_mode == "sequence"
    assert all(script.source.download_url is None for script in created.snapshot.scripts)
    assert len(created.shards) == 4
    assert {shard.script_binding_id for shard in created.shards} == {
        "01J00000000000000000000045",
        "01J00000000000000000000046",
    }
    assert all(shard.case_keys for shard in created.shards)

    with container.uow_factory()() as uow:
        loaded = uow.task_runs.get_by_run_id(RUN_ID.root)
        assert loaded is not None
        assert loaded.snapshot == created.snapshot
        assert loaded.task_revision == 1


def test_v2_task_service_rejects_invalid_binding_case(client) -> None:
    container = client.app.state.container
    _seed_project(container)
    service = container.v2_task_service()
    definition = service.register_script_definition(_definition(SCRIPT_A, name="alpha"))
    invalid = _binding("01J00000000000000000000047", definition.definition, 0).model_copy(
        update={"case_selection": CaseSelection(selected_keys=("missing",))}
    )
    task = ProtocolTestTask(
        task_id=BusinessId("01J00000000000000000000048"),
        project_id=PROJECT_ID,
        revision=1,
        name="invalid case",
        scripts=(invalid,),
    )

    try:
        service.create_task(task, created_by=definition.id or 1)
    except ValueError as exc:
        assert "不存在的用例" in str(exc)
    else:
        raise AssertionError("任务不应接受不存在的 case key")
