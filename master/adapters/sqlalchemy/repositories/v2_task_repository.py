"""SQLAlchemy V2 ScriptDefinition 和 TestTask revision 仓储。"""

from __future__ import annotations

from typing import Literal, cast

from aetp_protocol.artifacts import CaseSelection, Configuration, ScriptRef, TestCase
from aetp_protocol.execution import ExecutionRequirement, RetryPolicy, SplitPolicy
from aetp_protocol.ids import BusinessId, PluginId, SemVer, Sha256
from aetp_protocol.plugin_types import PluginRef
from aetp_protocol.task import ScriptDefinition, TaskScriptRef, TestTask
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from master.adapters.sqlalchemy.orm import (
    ScriptDefinition as ScriptDefinitionORM,
)
from master.adapters.sqlalchemy.orm import (
    TestTaskScript as TestTaskScriptORM,
)
from master.adapters.sqlalchemy.orm import (
    V2TestTask,
)
from master.domain.models import ScriptDefinitionRecord, V2TestTaskRecord
from master.domain.repositories import ScriptDefinitionRepository, V2TestTaskRepository


def _script_to_domain(orm: ScriptDefinitionORM) -> ScriptDefinitionRecord:
    definition = ScriptDefinition(
        script_definition_id=BusinessId(orm.script_definition_id),
        project_id=BusinessId(orm.project_id),
        revision=orm.revision,
        name=orm.name,
        executor=PluginRef(
            plugin_id=PluginId(orm.executor_plugin_id),
            version=SemVer(orm.executor_version),
            archive_sha256=Sha256(orm.executor_archive_sha256),
        ),
        source=ScriptRef.model_validate(orm.source),
        configuration=Configuration.model_validate(orm.configuration),
        cases=tuple(TestCase.model_validate(case) for case in orm.cases),
        requirement=(
            ExecutionRequirement.model_validate(orm.requirement)
            if orm.requirement is not None
            else None
        ),
        enabled=orm.enabled,
    )
    return ScriptDefinitionRecord(
        id=orm.id,
        definition=definition,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _task_to_domain(orm: V2TestTask) -> V2TestTaskRecord:
    scripts = tuple(
        TaskScriptRef(
            binding_id=BusinessId(item.binding_id),
            script_definition_id=BusinessId(item.script_definition_id),
            script_revision=item.script_revision,
            case_selection=CaseSelection.model_validate(item.case_selection),
            configuration=Configuration.model_validate(item.configuration),
            split_policy=SplitPolicy.model_validate(item.split_policy),
            order_index=item.order_index,
            enabled=item.enabled,
        )
        for item in sorted(orm.scripts, key=lambda script: (script.order_index, script.id))
    )
    task = TestTask(
        task_id=BusinessId(orm.task_id),
        project_id=BusinessId(orm.project_id),
        revision=orm.revision,
        name=orm.name,
        scripts=scripts,
        execution_mode=cast(Literal["parallel", "sequence"], orm.execution_mode),
        stop_on_failure=orm.stop_on_failure,
        retry_policy=RetryPolicy.model_validate(orm.retry_policy),
        node_ids=tuple(BusinessId(node_id) for node_id in orm.node_ids),
        priority=orm.priority,
        enabled=orm.enabled,
    )
    return V2TestTaskRecord(
        id=orm.id,
        task=task,
        created_by=orm.created_by,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class ScriptDefinitionRepositoryImpl(ScriptDefinitionRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, script_definition_id: BusinessId, revision: int) -> ScriptDefinitionRecord | None:
        orm = self._s.execute(
            select(ScriptDefinitionORM).where(
                ScriptDefinitionORM.script_definition_id == script_definition_id.root,
                ScriptDefinitionORM.revision == revision,
            )
        ).scalar_one_or_none()
        return _script_to_domain(orm) if orm is not None else None

    def list_by_project(self, project_id: BusinessId, *, enabled: bool | None = None) -> list[ScriptDefinitionRecord]:
        statement = select(ScriptDefinitionORM).where(ScriptDefinitionORM.project_id == project_id.root)
        if enabled is not None:
            statement = statement.where(ScriptDefinitionORM.enabled.is_(enabled))
        statement = statement.order_by(
            ScriptDefinitionORM.script_definition_id,
            ScriptDefinitionORM.revision.desc(),
        )
        return [_script_to_domain(item) for item in self._s.execute(statement).scalars().all()]

    def add(self, record: ScriptDefinitionRecord) -> ScriptDefinitionRecord:
        definition = record.definition
        orm = ScriptDefinitionORM(
            script_definition_id=definition.script_definition_id.root,
            project_id=definition.project_id.root,
            revision=definition.revision,
            name=definition.name,
            executor_plugin_id=definition.executor.plugin_id.root,
            executor_version=definition.executor.version.root,
            executor_archive_sha256=definition.executor.archive_sha256.root,
            source=definition.source.model_dump(mode="json"),
            configuration=definition.configuration.model_dump(mode="json"),
            cases=[case.model_dump(mode="json") for case in definition.cases],
            requirement=(
                definition.requirement.model_dump(mode="json")
                if definition.requirement is not None
                else None
            ),
            enabled=definition.enabled,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _script_to_domain(orm)


class V2TestTaskRepositoryImpl(V2TestTaskRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, task_id: BusinessId, revision: int | None = None) -> V2TestTaskRecord | None:
        statement = select(V2TestTask).options(selectinload(V2TestTask.scripts)).where(
            V2TestTask.task_id == task_id.root
        )
        if revision is not None:
            statement = statement.where(V2TestTask.revision == revision)
        else:
            statement = statement.order_by(V2TestTask.revision.desc())
        orm = self._s.execute(statement).scalars().first()
        return _task_to_domain(orm) if orm is not None else None

    def list_by_project(
        self,
        project_id: BusinessId,
        *,
        enabled: bool | None = None,
    ) -> list[V2TestTaskRecord]:
        statement = (
            select(V2TestTask)
            .options(selectinload(V2TestTask.scripts))
            .where(V2TestTask.project_id == project_id.root)
            .order_by(V2TestTask.task_id, V2TestTask.revision.desc())
        )
        if enabled is not None:
            statement = statement.where(V2TestTask.enabled.is_(enabled))
        return [
            _task_to_domain(orm)
            for orm in self._s.execute(statement).scalars().all()
        ]

    def add(self, record: V2TestTaskRecord) -> V2TestTaskRecord:
        task = record.task
        definition_keys = {
            (script.script_definition_id.root, script.script_revision)
            for script in task.scripts
        }
        definitions = {
            key: self._s.execute(
                select(ScriptDefinitionORM).where(
                    ScriptDefinitionORM.project_id == task.project_id.root,
                    ScriptDefinitionORM.script_definition_id == key[0],
                    ScriptDefinitionORM.revision == key[1],
                )
            ).scalar_one_or_none()
            for key in definition_keys
        }
        if any(definition is None for definition in definitions.values()):
            raise ValueError("TestTask 引用的 ScriptDefinition 不存在或项目范围不一致")
        if record.created_by <= 0:
            raise ValueError("缺少创建者 created_by")
        orm = V2TestTask(
            task_id=task.task_id.root,
            project_id=task.project_id.root,
            revision=task.revision,
            name=task.name,
            execution_mode=task.execution_mode,
            stop_on_failure=task.stop_on_failure,
            retry_policy=task.retry_policy.model_dump(mode="json"),
            node_ids=[node_id.root for node_id in task.node_ids],
            priority=task.priority,
            enabled=task.enabled,
            created_by=record.created_by,
        )
        orm.scripts = [
            TestTaskScriptORM(
                task_revision=task.revision,
                binding_id=script.binding_id.root,
                script_definition_id=script.script_definition_id.root,
                script_revision=script.script_revision,
                case_selection=script.case_selection.model_dump(mode="json"),
                configuration=script.configuration.model_dump(mode="json"),
                split_policy=script.split_policy.model_dump(mode="json"),
                order_index=script.order_index,
                enabled=script.enabled,
            )
            for script in task.scripts
        ]
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _task_to_domain(orm)


__all__ = ["ScriptDefinitionRepositoryImpl", "V2TestTaskRepositoryImpl"]
