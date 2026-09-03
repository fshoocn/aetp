""" 协议 JSON Schema 生成入口。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias

from pydantic import BaseModel

from .artifacts import ArtifactRef, Configuration, ConfigurationSchema, ScriptRef
from .authorization import AuthorizationDecision, AuthorizationRequest, Principal, ProjectScope
from .capabilities import NodeCapabilitySnapshot
from .envelope import Envelope
from .execution import ExecutionPlan, ExecutionRequirement, ExecutionResult, ResourceLease
from .payloads import PAYLOAD_MODELS, RemoteOperation
from .plugin_types import DesiredPluginVersion, PluginRef
from .plugins import PluginManifest
from .task import RunScriptSnapshot, RunSnapshot, ScriptDefinition, TaskScriptRef, TestRun, TestTask

SchemaModel: TypeAlias = type[BaseModel]

SCHEMA_MODELS: Mapping[str, SchemaModel] = {
    "envelope": Envelope,
    "plugin_manifest": PluginManifest,
    "plugin_ref": PluginRef,
    "desired_plugin_version": DesiredPluginVersion,
    "node_capability_snapshot": NodeCapabilitySnapshot,
    "execution_requirement": ExecutionRequirement,
    "execution_plan": ExecutionPlan,
    "resource_lease": ResourceLease,
    "execution_result": ExecutionResult,
    "artifact_ref": ArtifactRef,
    "script_ref": ScriptRef,
    "configuration": Configuration,
    "configuration_schema": ConfigurationSchema,
    "principal": Principal,
    "authorization_request": AuthorizationRequest,
    "authorization_decision": AuthorizationDecision,
    "project_scope": ProjectScope,
    "remote_operation": RemoteOperation,
    "script_definition": ScriptDefinition,
    "task_script_ref": TaskScriptRef,
    "test_task": TestTask,
    "run_script_snapshot": RunScriptSnapshot,
    "run_snapshot": RunSnapshot,
    "test_run": TestRun,
}

PAYLOAD_SCHEMAS: Mapping[str, SchemaModel] = {
    message_type.value: payload_model for message_type, payload_model in PAYLOAD_MODELS.items()
}


def generate_schemas() -> dict[str, dict[str, object]]:
    """生成稳定的模型名到 JSON Schema 映射，供 CI 快照工具调用。"""
    models = {**SCHEMA_MODELS, **PAYLOAD_SCHEMAS}
    return {name: model.model_json_schema() for name, model in sorted(models.items())}


def write_schema_snapshot(path: str | Path) -> None:
    """将当前模型生成的 Schema 写入确定性快照文件。"""
    snapshot_path = Path(path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(generate_schemas(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
