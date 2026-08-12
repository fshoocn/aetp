"""项目范围设备 API。"""

from __future__ import annotations

from fastapi import APIRouter

from master.api.v1.permissions import ProjectAccessDep
from master.api.v1.dependencies import DeviceServiceDep
from master.api.v1.schemas import DeviceOut

router = APIRouter(
    prefix="/projects/{project_id}/devices",
    tags=["v1-project-devices"],
)


@router.get("", response_model=list[DeviceOut])
def list_project_devices(
    project_id: str,
    access: ProjectAccessDep,
    service: DeviceServiceDep,
    online: bool | None = None,
) -> list[DeviceOut]:
    """查询项目绑定节点下的设备。"""
    devices = service.list_for_project(project_id, online=online)
    return [DeviceOut.model_validate(device) for device in devices]


@router.get("/{device_id}", response_model=DeviceOut)
def get_project_device(
    project_id: str,
    device_id: str,
    access: ProjectAccessDep,
    service: DeviceServiceDep,
) -> DeviceOut:
    """查询项目范围内的设备详情。"""
    device = service.get_for_project(project_id, device_id)
    return DeviceOut.model_validate(device)
