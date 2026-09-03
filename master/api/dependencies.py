"""API 依赖注入：容器、服务和当前用户。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from master.adapters.sqlalchemy.database_interface import DatabaseInterface
from master.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from master.adapters.sse.event_bus import EventBus
from master.application.services.artifact_service import ArtifactService
from master.application.services.auth_service import AuthService
from master.application.services.event_publisher import EventPublisher
from master.application.services.execution_service import ExecutionService
from master.application.services.idempotency_service import IdempotencyService
from master.application.services.node_service import NodeService
from master.application.services.notification_service import NotificationService
from master.application.services.project_member_service import ProjectMemberService
from master.application.services.project_node_binding_service import (
    ProjectNodeBindingService,
)
from master.application.services.project_service import ProjectService
from master.application.services.schedule_service import ScheduleService
from master.application.services.scheduler_service import SchedulerService
from master.application.services.script_definition_service import ScriptDefinitionService
from master.application.services.script_storage_service import ScriptStorageService
from master.application.services.task_service import TaskService
from master.bootstrap.container import Container
from master.domain.enums import AccountStatus
from master.domain.models import User
from master.domain.repositories import UnitOfWork

from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


def get_container(request: Request) -> Container:
    """从 app.state 获取依赖注入容器。"""
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="应用未初始化",
        )
    return container


def get_database(
    container: Annotated[Container, Depends(get_container)],
) -> DatabaseInterface:
    """从容器解析数据库单例。"""
    return container.database()


def get_uow_factory(
    container: Annotated[Container, Depends(get_container)],
) -> SqlAlchemyUnitOfWorkFactory:
    """从容器解析工作单元工厂（可调用，返回新 UoW 实例）。"""
    return container.uow_factory()


def get_event_bus(
    container: Annotated[Container, Depends(get_container)],
) -> EventBus:
    """从容器解析 SSE 事件总线单例。"""
    return container.event_bus()


def get_event_publisher(
    container: Annotated[Container, Depends(get_container)],
) -> EventPublisher:
    """获取持久化领域事件发布器（P7.1）。"""
    return container.event_publisher()


def get_auth_service(
    container: Annotated[Container, Depends(get_container)],
) -> AuthService:
    """从容器解析认证服务。"""
    return container.auth_service()


def get_idempotency_service(
    container: Annotated[Container, Depends(get_container)],
) -> IdempotencyService:
    """获取写 API 持久化幂等服务。"""
    return container.idempotency_service()


def get_artifact_service(
    container: Annotated[Container, Depends(get_container)],
) -> ArtifactService:
    """从容器解析产物服务（P6.6）。"""
    return container.artifact_service()


def get_project_service(
    container: Annotated[Container, Depends(get_container)],
) -> ProjectService:
    """从容器解析项目服务。"""
    return container.project_service()


def get_project_member_service(
    container: Annotated[Container, Depends(get_container)],
) -> ProjectMemberService:
    """从容器解析项目成员服务。"""
    return container.project_member_service()


def get_project_node_binding_service(
    container: Annotated[Container, Depends(get_container)],
) -> ProjectNodeBindingService:
    """从容器解析项目节点绑定服务。"""
    return container.project_node_binding_service()


def get_node_service(
    container: Annotated[Container, Depends(get_container)],
) -> NodeService:
    """从容器解析 Node 查询服务。"""
    return container.node_service()


def get_script_storage_service(
    container: Annotated[Container, Depends(get_container)],
) -> ScriptStorageService:
    """从容器解析脚本文件存储服务（P4.7）。"""
    return container.script_storage_service()


def get_scheduler_service(
    container: Annotated[Container, Depends(get_container)],
) -> SchedulerService:
    """获取  Snapshot 调度服务。"""
    return container.scheduler_service()


def get_task_service(
    container: Annotated[Container, Depends(get_container)],
) -> TaskService:
    """获取当前多脚本任务与 Run 服务。"""
    return container.task_service()


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    uow_factory: Annotated[Callable[[], UnitOfWork], Depends(get_uow_factory)],
) -> User:
    """解析 Bearer 令牌并加载当前 active 用户。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    with uow_factory() as uow:
        user = uow.users.get_by_id(user_id)

    if user is None or user.account_status != AccountStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在、未审批或已禁用",
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbDep = Annotated[DatabaseInterface, Depends(get_database)]
UowFactoryDep = Annotated[SqlAlchemyUnitOfWorkFactory, Depends(get_uow_factory)]
EventBusDep = Annotated[EventBus, Depends(get_event_bus)]
EventPublisherDep = Annotated[EventPublisher, Depends(get_event_publisher)]
AuthDep = Annotated[AuthService, Depends(get_auth_service)]
IdempotencyServiceDep = Annotated[IdempotencyService, Depends(get_idempotency_service)]
ArtifactServiceDep = Annotated[ArtifactService, Depends(get_artifact_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
ProjectMemberServiceDep = Annotated[ProjectMemberService, Depends(get_project_member_service)]
ProjectNodeBindingServiceDep = Annotated[ProjectNodeBindingService, Depends(get_project_node_binding_service)]
NodeServiceDep = Annotated[NodeService, Depends(get_node_service)]
ScriptStorageServiceDep = Annotated[ScriptStorageService, Depends(get_script_storage_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
SchedulerServiceDep = Annotated[SchedulerService, Depends(get_scheduler_service)]


def get_execution_service(
    container: Annotated[Container, Depends(get_container)],
) -> ExecutionService:
    """获取  执行投影与取消服务。"""
    return container.execution_service()


ExecutionServiceDep = Annotated[ExecutionService, Depends(get_execution_service)]


def get_script_definition_service(
    container: Annotated[Container, Depends(get_container)],
) -> ScriptDefinitionService:
    """获取  ScriptDefinition 上传/解析服务。"""
    return container.script_definition_service()


ScriptDefinitionServiceDep = Annotated[
    ScriptDefinitionService,
    Depends(get_script_definition_service),
]


def get_notification_service(
    container: Annotated[Container, Depends(get_container)],
) -> NotificationService:
    """从容器解析通知管理服务（P7.6）。"""
    return container.notification_service()


NotificationServiceDep = Annotated[NotificationService, Depends(get_notification_service)]


def get_schedule_service(
    container: Annotated[Container, Depends(get_container)],
) -> ScheduleService:
    """从容器解析调度计划服务（P8.2）。"""
    return container.schedule_service()


ScheduleServiceDep = Annotated[ScheduleService, Depends(get_schedule_service)]


