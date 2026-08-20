"""任务业务服务。

负责任务的创建（生成 task_id、落库、MQTT 派发由后续集成实现）、查询与日志。
使用领域对象 Task（含状态机）与仓储，不直接操作 Session。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from aetp_protocol.ids import new_id

from master.application.errors import DeviceNotFoundError, TaskNotFoundError
from master.domain.models import Task, TaskLog
from master.domain.repositories import UnitOfWork

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    # ---- 增 ----

    def create(
        self,
        project_id: str,
        device_id: str,
        command: dict,
        created_by: int,
    ) -> Task:
        """创建任务（pending 状态）；MQTT 派发由后续集成实现。

        command 为结构化 JSON 对象（非字符串），并记录创建人 created_by。
        """
        with self._uow_factory() as uow:
            device = uow.devices.get_for_project(project_id, device_id)
            if device is None:
                raise DeviceNotFoundError("设备不存在或不属于当前项目")

            task = Task.create(
                task_id=new_id(),
                project_id=project_id,
                device_id=device_id,
                command=command,
                created_by=created_by,
            )
            created = uow.tasks.add(task)
            logger.info(
                "任务创建成功: task_id=%s, project_id=%s, device_id=%s, created_by=%s",
                created.task_id,
                project_id,
                device_id,
                created_by,
            )
            return created

    # ---- 查 ----

    def list_all(
        self,
        device_id: str | None = None,
        status: str | None = None,
        project_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Task]:
        """列出任务；可选按 device_id / status 过滤，支持分页。"""
        with self._uow_factory() as uow:
            tasks = uow.tasks.list(
                project_id=project_id,
                device_id=device_id,
                status=status,
                limit=limit,
                offset=offset,
            )
            logger.debug(
                "查询任务列表: project_id=%s, device_id=%s, status=%s, count=%s",
                project_id,
                device_id,
                status,
                len(tasks),
            )
            return tasks

    def get_by_id(self, task_id: str, project_id: str | None = None) -> Task | None:
        """按业务 task_id 查询。"""
        with self._uow_factory() as uow:
            task = uow.tasks.get_by_task_id(task_id, project_id=project_id)
            logger.debug(
                "查询任务详情: task_id=%s, project_id=%s, found=%s",
                task_id,
                project_id,
                task is not None,
            )
            return task

    def get_logs(self, task_id: str, project_id: str | None = None) -> list[TaskLog]:
        """查询任务日志（按序号升序）；任务不存在抛 TaskNotFoundError。"""
        with self._uow_factory() as uow:
            if uow.tasks.get_by_task_id(task_id, project_id=project_id) is None:
                raise TaskNotFoundError("任务不存在")
            logs = uow.task_logs.list_by_task(task_id, project_id=project_id)
            logger.debug(
                "查询任务日志: task_id=%s, project_id=%s, count=%s",
                task_id,
                project_id,
                len(logs),
            )
            return logs
