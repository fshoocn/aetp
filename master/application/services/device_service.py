"""设备业务服务。

负责设备查询，不包含设备上下线逻辑（由 MQTT 适配器负责）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from master.application.errors import DeviceNotFoundError
from master.domain.models import Device
from master.domain.repositories import UnitOfWork

logger = logging.getLogger(__name__)


class DeviceService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def list_all(self, online: bool | None = None) -> list[Device]:
        """列出设备；可选 ?online=true 过滤。"""
        with self._uow_factory() as uow:
            devices = uow.devices.list_all(online=online)
            logger.debug("查询全局设备列表: online=%s, count=%s", online, len(devices))
            return devices

    def get_by_id(self, device_id: str) -> Device | None:
        """按 device_id 查询。"""
        with self._uow_factory() as uow:
            device = uow.devices.get_by_id(device_id)
            logger.debug(
                "查询设备详情: device_id=%s, found=%s",
                device_id,
                device is not None,
            )
            return device

    def list_for_project(
        self,
        project_id: str,
        online: bool | None = None,
    ) -> list[Device]:
        """只查询项目已绑定且启用节点下的设备。"""
        with self._uow_factory() as uow:
            devices = uow.devices.list_for_project(project_id, online=online)
            logger.debug(
                "查询项目设备列表: project_id=%s, online=%s, count=%s",
                project_id,
                online,
                len(devices),
            )
            return devices

    def get_for_project(self, project_id: str, device_id: str) -> Device:
        """按项目范围查询设备；未绑定或不存在统一视为不存在。"""
        with self._uow_factory() as uow:
            device = uow.devices.get_for_project(project_id, device_id)
            if device is None:
                raise DeviceNotFoundError("设备不存在或不属于当前项目")
            logger.debug(
                "项目设备访问通过: project_id=%s, device_id=%s",
                project_id,
                device_id,
            )
            return device
