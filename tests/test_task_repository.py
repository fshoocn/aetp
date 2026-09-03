""" ScriptDefinition 和多脚本 TestTask revision 仓储测试。"""

from __future__ import annotations

from aetp_protocol.artifacts import CaseSelection, Configuration, ScriptRef
from aetp_protocol.artifacts import TestCase as ProtocolTestCase
from aetp_protocol.execution import SplitPolicy
from aetp_protocol.ids import BusinessId, PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import PluginRef
from aetp_protocol.task import TaskScriptRef
from aetp_protocol.task import TestTask as ProtocolTestTask

from master.domain.models import ScriptDefinitionRecord, TestTaskRecord
from tests.test_m3_plan_lease import NOW

PROJECT_ID = BusinessId("01J00000000000000000000030")
SCRIPT_ID = BusinessId("01J00000000000000000000031")
TASK_ID = BusinessId("01J00000000000000000000032")


def _definition(revision: int = 1) -> ScriptDefinitionRecord:
    from aetp_protocol.task import ScriptDefinition

    definition = ScriptDefinition(
        script_definition_id=SCRIPT_ID,
        project_id=PROJECT_ID,
        revision=revision,
        name="smoke",
        executor=PluginRef(
            plugin_id=PluginId("org.pytest.executor"),
            version=SemVer("2.0.0"),
            archive_sha256=Sha256("a" * 64),
        ),
        source=ScriptRef(
            script_id=SCRIPT_ID,
            version=revision,
            filename="tests.zip",
            size=10,
            sha256=Sha256("b" * 64),
        ),
        configuration=Configuration(schema_version=1, schema_hash=Sha256("c" * 64), values={}),
        cases=(
            ProtocolTestCase(stable_key="case-a", name="Case A"),
            ProtocolTestCase(stable_key="case-b", name="Case B"),
        ),
    )
    return ScriptDefinitionRecord(id=None, definition=definition, created_at=NOW, updated_at=NOW)


def _task(script_revision: int = 1) -> TestTaskRecord:
    script = TaskScriptRef(
        binding_id=BusinessId("01J00000000000000000000033"),
        script_definition_id=SCRIPT_ID,
        script_revision=script_revision,
        case_selection=CaseSelection(selected_keys=("case-a",)),
        configuration=Configuration(schema_version=1, schema_hash=Sha256("c" * 64), values={}),
        split_policy=SplitPolicy(type="none"),
        order_index=0,
    )
    return TestTaskRecord(
        id=None,
        task=ProtocolTestTask(
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            revision=script_revision,
            name="smoke-task",
            scripts=(script,),
        ),
        created_by=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _seed_user_and_project(container) -> None:
    from master.domain.enums import AccountStatus, PlatformRole, ProjectStatus
    from master.domain.models import Project, User

    with container.uow_factory()() as uow:
        user = uow.users.add(
            User(
                id=None,
                username="v2-task-owner",
                password_hash="hash",
                display_name=" Task Owner",
                account_status=AccountStatus.ACTIVE,
                platform_role=PlatformRole.ADMIN,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        uow.projects.add(
            Project(
                id=None,
                project_id=PROJECT_ID.root,
                project_key="TASK",
                name=" Task",
                description="",
                status=ProjectStatus.ACTIVE,
                created_by=user.id,
                created_at=NOW,
                updated_at=NOW,
            )
        )


def test_definition_and_task_repository_round_trip(client) -> None:
    container = client.app.state.container
    _seed_user_and_project(container)
    with container.uow_factory()() as uow:
        definition = uow.script_definitions.add(_definition())
        stored = uow.test_tasks.add(_task())
        loaded_definition = uow.script_definitions.get(SCRIPT_ID, 1)
        loaded_task = uow.test_tasks.get(TASK_ID, 1)

        assert definition.definition == loaded_definition.definition
        assert stored.task == loaded_task.task
        assert loaded_task.task.scripts[0].binding_id.root.endswith("33")
        assert loaded_task.task.scripts[0].order_index == 0


def test_task_repository_rejects_missing_script_revision(client) -> None:
    container = client.app.state.container
    _seed_user_and_project(container)
    with container.uow_factory()() as uow:
        uow.script_definitions.add(_definition())
        try:
            uow.test_tasks.add(_task(script_revision=2))
        except ValueError as exc:
            assert "ScriptDefinition" in str(exc)
        else:
            raise AssertionError("任务不应引用缺失的 ScriptDefinition revision")
