""" ScriptDefinition、TestTask 和 Run Snapshot 应用服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from aetp_protocol.artifacts import CaseSelection
from aetp_protocol.execution import (
    ExecutionRequirement,
    PluginRequirement,
    SplitPolicy,
    TriggerType,
)
from aetp_protocol.ids import BusinessId, VersionRange, new_id
from aetp_protocol.task import RunScriptSnapshot, RunSnapshot, ScriptDefinition, TestTask

from master.domain.models import RunShard, ScriptDefinitionRecord, TaskRun, TestTaskRecord
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow


@dataclass(frozen=True)
class RunCreated:
    """一次  Run 创建结果。"""

    run: TaskRun
    snapshot: RunSnapshot
    shards: tuple[RunShard, ...]


class TaskService:
    """管理  定义 revision，并原子创建多脚本 Run。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or utcnow

    def register_script_definition(self, definition: ScriptDefinition) -> ScriptDefinitionRecord:
        """登记不可变 ScriptDefinition revision；持久化前移除临时下载 URL。"""
        normalized = definition.model_copy(
            update={"source": definition.source.model_copy(update={"download_url": None})}
        )
        with self._uow_factory() as uow:
            existing = uow.script_definitions.get(
                normalized.script_definition_id,
                normalized.revision,
            )
            if existing is not None:
                if existing.definition != normalized:
                    raise ValueError("ScriptDefinition revision 已存在但内容不同")
                return existing
            if uow.projects.get_by_project_id(normalized.project_id.root) is None:
                raise KeyError(f"项目不存在: {normalized.project_id.root}")
            return uow.script_definitions.add(
                ScriptDefinitionRecord(
                    id=None,
                    definition=normalized,
                    created_at=self._now(),
                    updated_at=self._now(),
                )
            )

    def create_task(
        self,
        task: TestTask,
        *,
        created_by: int,
    ) -> TestTaskRecord:
        """校验脚本 revision 和绑定配置后创建不可变 TestTask revision。"""
        with self._uow_factory() as uow:
            if uow.projects.get_by_project_id(task.project_id.root) is None:
                raise KeyError(f"项目不存在: {task.project_id.root}")
            self._validate_task_scripts(uow, task)
            existing = uow.test_tasks.get(task.task_id, task.revision)
            if existing is not None:
                if existing.task != task:
                    raise ValueError("TestTask revision 已存在但内容不同")
                return existing
            return uow.test_tasks.add(
                TestTaskRecord(
                    id=None,
                    task=task,
                    created_by=created_by,
                    created_at=self._now(),
                    updated_at=self._now(),
                )
            )

    def create_run(
        self,
        task_id: BusinessId,
        *,
        project_id: BusinessId,
        task_revision: int | None = None,
        trigger_type: TriggerType = TriggerType.MANUAL_WEB,
        run_id: BusinessId | None = None,
        original_run_id: BusinessId | None = None,
        case_filter: set[str] | None = None,
    ) -> RunCreated:
        """将一个  TestTask revision 快照化并展开为多个脚本 Shard。"""
        with self._uow_factory() as uow:
            task_record = uow.test_tasks.get(task_id, task_revision)
            if task_record is None or task_record.task.project_id != project_id:
                raise KeyError(f" TestTask 不存在或不属于项目: {task_id.root}")
            task = task_record.task
            if not task.enabled:
                raise ValueError(" TestTask 已停用，不能创建 Run")

            selected_snapshots: list[RunScriptSnapshot] = []
            shard_inputs: list[tuple[BusinessId, list[str]]] = []
            for binding in sorted(task.scripts, key=lambda item: item.order_index):
                if not binding.enabled:
                    continue
                definition_record = uow.script_definitions.get(
                    binding.script_definition_id,
                    binding.script_revision,
                )
                if definition_record is None or definition_record.definition.project_id != project_id:
                    raise ValueError(
                        "TestTask 引用的 ScriptDefinition revision 不存在或项目范围不一致"
                    )
                definition = definition_record.definition
                if not definition.enabled:
                    raise ValueError(
                        f"ScriptDefinition 已停用: {binding.script_definition_id.root}@{binding.script_revision}"
                    )
                if binding.configuration.schema_hash != definition.configuration.schema_hash:
                    raise ValueError("任务脚本配置 Schema hash 与 ScriptDefinition 不一致")
                selected_keys = self._selected_case_keys(binding.case_selection, definition)
                if case_filter is not None:
                    selected_keys = tuple(key for key in selected_keys if key in case_filter)
                if not selected_keys:
                    continue
                requirement = self._default_requirement(definition)
                source = definition.source.model_copy(update={"download_url": None})
                selected_snapshots.append(
                    RunScriptSnapshot(
                        binding_id=binding.binding_id,
                        script_definition_id=definition.script_definition_id,
                        script_revision=definition.revision,
                        executor=definition.executor,
                        source=source,
                        configuration=binding.configuration,
                        requirement=requirement,
                        selected_case_keys=selected_keys,
                        split_policy=binding.split_policy,
                    )
                )
                for case_keys in self._split_cases(selected_keys, definition, binding.split_policy):
                    shard_inputs.append((binding.binding_id, list(case_keys)))

            if not selected_snapshots:
                raise ValueError(" TestTask 没有启用的脚本")
            snapshot = RunSnapshot(
                task_id=task.task_id,
                task_revision=task.revision,
                scripts=tuple(selected_snapshots),
                execution_mode=task.execution_mode,
                stop_on_failure=task.stop_on_failure,
                retry_policy=task.retry_policy,
                node_ids=task.node_ids,
                trigger_type=trigger_type,
                original_run_id=original_run_id,
            )
            actual_run_id = run_id or BusinessId(new_id())
            existing_run = uow.task_runs.get_by_run_id(actual_run_id.root)
            if existing_run is not None:
                if existing_run.snapshot != snapshot:
                    raise ValueError("run_id 已用于不同的  Run Snapshot")
                return RunCreated(
                    run=existing_run,
                    snapshot=snapshot,
                    shards=tuple(uow.run_shards.list_by_run(actual_run_id.root)),
                )

            run = uow.task_runs.add(
                TaskRun(
                    run_id=actual_run_id.root,
                    project_id=project_id.root,
                    task_id=task.task_id.root,
                    task_revision=task.revision,
                    snapshot=snapshot,
                    trigger_type=trigger_type,
                )
            )
            shards = uow.run_shards.add_many(
                [
                    RunShard(
                        shard_id=BusinessId(new_id()).root,
                        run_id=run.run_id,
                        script_binding_id=binding_id.root,
                        shard_index=index,
                        case_keys=case_keys,
                    )
                    for index, (binding_id, case_keys) in enumerate(shard_inputs)
                ]
            )
            return RunCreated(
                run=run,
                snapshot=run.snapshot or snapshot,
                shards=tuple(shards),
            )

    @staticmethod
    def _validate_task_scripts(uow: UnitOfWork, task: TestTask) -> None:
        for binding in task.scripts:
            definition_record = uow.script_definitions.get(
                binding.script_definition_id,
                binding.script_revision,
            )
            if definition_record is None or definition_record.definition.project_id != task.project_id:
                raise ValueError("TestTask 引用的 ScriptDefinition revision 不存在或项目范围不一致")
            definition = definition_record.definition
            if not definition.enabled:
                raise ValueError("TestTask 不能绑定已停用的 ScriptDefinition")
            if binding.configuration.schema_hash != definition.configuration.schema_hash:
                raise ValueError("任务脚本配置 Schema hash 与 ScriptDefinition 不一致")
            TaskService._selected_case_keys(binding.case_selection, definition)

    @staticmethod
    def _selected_case_keys(selection: CaseSelection, definition: ScriptDefinition) -> tuple[str, ...]:
        available = {case.stable_key for case in definition.cases}
        selected = (
            tuple(case.stable_key for case in definition.cases)
            if selection.include_all
            else selection.selected_keys
        )
        invalid = tuple(key for key in selected if key not in available)
        if invalid:
            raise ValueError(
                "任务脚本选择了不存在的用例: " + ", ".join(invalid[:5])
            )
        return selected

    @staticmethod
    def _split_cases(
        selected_keys: tuple[str, ...],
        definition: ScriptDefinition,
        policy: SplitPolicy,
    ) -> tuple[tuple[str, ...], ...]:
        if policy.type == "none":
            return (selected_keys,)
        if policy.type == "by_case_count":
            assert policy.target_count is not None
            return tuple(
                selected_keys[index : index + policy.target_count]
                for index in range(0, len(selected_keys), policy.target_count)
            )
        if policy.type == "by_time":
            assert policy.target_duration_s is not None
            durations = {case.stable_key: case.estimated_duration_s for case in definition.cases}
            if any(durations[key] is None for key in selected_keys):
                raise ValueError("按时间分片要求所有选中用例具备 estimated_duration_s")
            chunks: list[list[str]] = []
            current: list[str] = []
            current_duration = 0.0
            for key in selected_keys:
                duration = durations[key] or 0.0
                if current and current_duration + duration > policy.target_duration_s:
                    chunks.append(current)
                    current = []
                    current_duration = 0.0
                current.append(key)
                current_duration += duration
            if current:
                chunks.append(current)
            return tuple(tuple(chunk) for chunk in chunks)
        raise ValueError("custom 分片必须由已解析的  sharding 插件生成")

    @staticmethod
    def _default_requirement(definition: ScriptDefinition) -> ExecutionRequirement:
        if definition.requirement is not None:
            return definition.requirement
        return ExecutionRequirement(
            executor=PluginRequirement(
                plugin_id=definition.executor.plugin_id,
                version=VersionRange(exact=definition.executor.version),
            )
        )


__all__ = ["RunCreated", "TaskService"]
