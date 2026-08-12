"""平台 Node/Device 只读查询路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from master.api.v1.schemas import NodeOut
from master.api.v1.dependencies import CurrentUser, DeviceServiceDep, NodeServiceDep
from master.api.v1.schemas import DeviceOut

router = APIRouter(tags=["v1-assets"])


@router.get("/nodes", response_model=list[NodeOut])
def list_nodes(
    service: NodeServiceDep,
    _current_user: CurrentUser,
    online: bool | None = None,
    enabled: bool | None = None,
) -> list[NodeOut]:
    """列出平台 Node；所有已激活用户可只读查看。"""
    nodes = service.list_all(online=online, enabled=enabled)
    return [NodeOut.model_validate(node) for node in nodes]


@router.get("/nodes/{node_id}", response_model=NodeOut)
def get_node(
    node_id: str,
    service: NodeServiceDep,
    _current_user: CurrentUser,
) -> NodeOut:
    """查询 Node 详情及其 Device 列表。"""
    node = service.get_by_id(node_id)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点不存在",
        )
    return NodeOut.model_validate(node)


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(
    service: DeviceServiceDep,
    _current_user: CurrentUser,
    online: bool | None = None,
) -> list[DeviceOut]:
    """列出平台 Device；所有已激活用户可只读查看。"""
    devices = service.list_all(online=online)
    return [DeviceOut.model_validate(device) for device in devices]


@router.get("/devices/{device_id}", response_model=DeviceOut)
def get_device(
    device_id: str,
    service: DeviceServiceDep,
    _current_user: CurrentUser,
) -> DeviceOut:
    """查询 Device 详情。"""
    device = service.get_by_id(device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="设备不存在",
        )
    return DeviceOut.model_validate(device)