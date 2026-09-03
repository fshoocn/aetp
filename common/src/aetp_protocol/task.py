""" ScriptDefinition、TestTask 和 Run Snapshot 契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import CaseSelection, Configuration, ScriptRef, TestCase
from .execution import ExecutionRequirement, RetryPolicy, RunStatus, SplitPolicy, TriggerType
from .ids import BusinessId
from .plugin_types import PluginRef


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScriptDefinition(_Strict):
    script_definition_id: BusinessId
    project_id: BusinessId
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=255)
    executor: PluginRef
    source: ScriptRef
    configuration: Configuration
    cases: tuple[TestCase, ...]
    requirement: ExecutionRequirement | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_cases(self) -> ScriptDefinition:
        keys = tuple(case.stable_key for case in self.cases)
        if len(keys) != len(set(keys)):
            raise ValueError("script case stable_key must be unique")
        return self


class TaskScriptRef(_Strict):
    binding_id: BusinessId
    script_definition_id: BusinessId
    script_revision: int = Field(ge=1)
    case_selection: CaseSelection
    configuration: Configuration
    split_policy: SplitPolicy
    order_index: int = Field(ge=0)
    enabled: bool = True


class TestTask(_Strict):
    task_id: BusinessId
    project_id: BusinessId
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=255)
    scripts: tuple[TaskScriptRef, ...]
    execution_mode: Literal["parallel", "sequence"] = "parallel"
    stop_on_failure: bool = False
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    node_ids: tuple[BusinessId, ...] = ()
    priority: int = 0
    enabled: bool = True

    @model_validator(mode="after")
    def validate_scripts(self) -> TestTask:
        if not self.scripts:
            raise ValueError("TestTask must contain at least one script")
        binding_ids = tuple(script.binding_id.root for script in self.scripts)
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("task script binding_id must be unique")
        order_indexes = tuple(script.order_index for script in self.scripts)
        if len(order_indexes) != len(set(order_indexes)):
            raise ValueError("task script order_index must be unique")
        return self


class RunScriptSnapshot(_Strict):
    binding_id: BusinessId
    script_definition_id: BusinessId
    script_revision: int = Field(ge=1)
    executor: PluginRef
    source: ScriptRef
    configuration: Configuration
    requirement: ExecutionRequirement
    selected_case_keys: tuple[str, ...]
    split_policy: SplitPolicy

    @model_validator(mode="after")
    def validate_case_keys(self) -> RunScriptSnapshot:
        if len(self.selected_case_keys) != len(set(self.selected_case_keys)):
            raise ValueError("RunScriptSnapshot selected_case_keys must be unique")
        return self


class RunSnapshot(_Strict):
    task_id: BusinessId
    task_revision: int = Field(ge=1)
    scripts: tuple[RunScriptSnapshot, ...]
    execution_mode: Literal["parallel", "sequence"]
    stop_on_failure: bool
    retry_policy: RetryPolicy
    node_ids: tuple[BusinessId, ...] = ()
    trigger_type: TriggerType
    original_run_id: BusinessId | None = None

    @model_validator(mode="after")
    def validate_scripts(self) -> RunSnapshot:
        if not self.scripts:
            raise ValueError("RunSnapshot must contain at least one script")
        bindings = tuple(script.binding_id.root for script in self.scripts)
        if len(bindings) != len(set(bindings)):
            raise ValueError("RunSnapshot binding_id must be unique")
        return self


class TestRun(_Strict):
    run_id: BusinessId
    project_id: BusinessId
    snapshot: RunSnapshot
    status: RunStatus
    created_at: datetime


for model in (ScriptDefinition, TaskScriptRef, TestTask, RunScriptSnapshot, RunSnapshot, TestRun):
    model.model_rebuild()


__all__ = [
    "RunScriptSnapshot",
    "RunSnapshot",
    "ScriptDefinition",
    "TaskScriptRef",
    "TestRun",
    "TestTask",
]
