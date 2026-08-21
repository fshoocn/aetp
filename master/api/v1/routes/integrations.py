"""项目范围 CI/CD 集成 API（P8.3，§8.8）。

集成 CRUD（项目 owner）+ 触发绑定管理（maintainer）+ webhook 入口。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from master.api.v1.dependencies import CiIntegrationServiceDep
from master.api.v1.permissions import (
    ProjectAccessDep,
    ProjectManagerDep,
    ProjectOwnerDep,
)
from master.api.v1.schemas import (
    BindingCreateRequest,
    BindingOut,
    BindingUpdateRequest,
    IntegrationCreateRequest,
    IntegrationOut,
    IntegrationUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/integrations",
    tags=["v1-ci-integrations"],
)


@router.get("", response_model=list[IntegrationOut])
def list_integrations(
    project_id: str,
    _access: ProjectAccessDep,
    service: CiIntegrationServiceDep,
) -> list[IntegrationOut]:
    integrations = service.list_integrations(project_id)
    return [IntegrationOut.model_validate(i) for i in integrations]


@router.post("", response_model=IntegrationOut, status_code=status.HTTP_201_CREATED)
def create_integration(
    project_id: str,
    body: IntegrationCreateRequest,
    access: ProjectOwnerDep,
    service: CiIntegrationServiceDep,
) -> IntegrationOut:
    try:
        integration = service.create_integration(
            project_id=project_id,
            provider=body.provider,
            name=body.name,
            secret_value=body.secret_value,
            config_json=body.config_json,
            enabled=body.enabled,
            created_by=access.user.persisted_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return IntegrationOut.model_validate(integration)


@router.get("/{integration_id}", response_model=IntegrationOut)
def get_integration(
    project_id: str,
    integration_id: str,
    _access: ProjectAccessDep,
    service: CiIntegrationServiceDep,
) -> IntegrationOut:
    integration = service.get_integration(integration_id, project_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="集成不存在")
    return IntegrationOut.model_validate(integration)


@router.patch("/{integration_id}", response_model=IntegrationOut)
def update_integration(
    project_id: str,
    integration_id: str,
    body: IntegrationUpdateRequest,
    _access: ProjectOwnerDep,
    service: CiIntegrationServiceDep,
) -> IntegrationOut:
    try:
        integration = service.update_integration(
            integration_id,
            project_id=project_id,
            name=body.name,
            secret_value=body.secret_value,
            config_json=body.config_json,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return IntegrationOut.model_validate(integration)


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(
    project_id: str,
    integration_id: str,
    _access: ProjectOwnerDep,
    service: CiIntegrationServiceDep,
) -> None:
    try:
        service.delete_integration(integration_id, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# -- 触发绑定 ---------------------------------------------------------------


@router.get("/{integration_id}/bindings", response_model=list[BindingOut])
def list_bindings(
    project_id: str,
    integration_id: str,
    _access: ProjectAccessDep,
    service: CiIntegrationServiceDep,
) -> list[BindingOut]:
    bindings = service.list_bindings(integration_id)
    return [BindingOut.model_validate(b) for b in bindings]


@router.post(
    "/{integration_id}/bindings",
    response_model=BindingOut,
    status_code=status.HTTP_201_CREATED,
)
def create_binding(
    project_id: str,
    integration_id: str,
    body: BindingCreateRequest,
    _access: ProjectManagerDep,
    service: CiIntegrationServiceDep,
) -> BindingOut:
    try:
        binding = service.create_binding(
            integration_id=integration_id,
            task_id=body.task_id,
            event_filter_json=body.event_filter_json,
            parameter_mapping_json=body.parameter_mapping_json,
        )
    except (ValueError, Exception) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return BindingOut.model_validate(binding)


@router.patch(
    "/{integration_id}/bindings/{binding_id}",
    response_model=BindingOut,
)
def update_binding(
    project_id: str,
    integration_id: str,
    binding_id: str,
    body: BindingUpdateRequest,
    _access: ProjectManagerDep,
    service: CiIntegrationServiceDep,
) -> BindingOut:
    try:
        binding = service.update_binding(
            binding_id,
            event_filter_json=body.event_filter_json,
            parameter_mapping_json=body.parameter_mapping_json,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return BindingOut.model_validate(binding)


@router.delete(
    "/{integration_id}/bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_binding(
    project_id: str,
    integration_id: str,
    binding_id: str,
    _access: ProjectManagerDep,
    service: CiIntegrationServiceDep,
) -> None:
    try:
        service.delete_binding(binding_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
