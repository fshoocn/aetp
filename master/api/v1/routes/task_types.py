"""任务类型插件配置上下文 API（P7.2）。"""

from __future__ import annotations

from fastapi import APIRouter

from master.api.v1.dependencies import (
    PluginRegistryDep,
    ProjectNodeBindingServiceDep,
)
from master.api.v1.permissions import ProjectAccessDep

router = APIRouter(
    prefix="/projects/{project_id}/task-types",
    tags=["v1-project-task-types"],
)


@router.get("/{task_type}/config-context")
def get_config_context(
    project_id: str,
    task_type: str,
    _access: ProjectAccessDep,
    registry: PluginRegistryDep,
    binding_service: ProjectNodeBindingServiceDep,
) -> dict:
    """给插件 UI 提供项目节点能力和验证能力上下文。"""
    package = registry.require(task_type)
    bindings = binding_service.list_bindings(project_id)
    agent = package.agent
    verify_location = getattr(agent, "verify_location", "master")
    return {
        "project_id": project_id,
        "task_type": package.metadata.task_type,
        "plugin_version": package.metadata.plugin_version,
        "config_schema": dict(package.metadata.config_schema or {}),
        "upload_spec": dict(package.metadata.upload_spec or {}),
        "ui": dict(package.metadata.ui or {}),
        "nodes": [
            {
                "node_id": binding.node_id,
                "name": binding.name,
                "hostname": binding.hostname,
                "status": binding.status,
                "online": binding.online,
                "enabled": binding.enabled and binding.node_enabled,
                "capabilities": binding.capabilities.model_dump(mode="json"),
                "plugin_versions": dict(binding.plugin_versions),
            }
            for binding in bindings
        ],
        "verification": {
            "supported": verify_location == "agent",
            "location": verify_location,
            "endpoint_template": (
                f"/api/v1/projects/{project_id}/scripts/{{script_id}}/verify"
            ),
        },
    }
