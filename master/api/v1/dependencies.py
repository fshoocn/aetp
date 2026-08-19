"""v1 API 依赖注入：容器、服务和当前用户。"""

from __future__ import annotations

from typing import Annotated, Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from master.adapters.sqlalchemy.database_interface import DatabaseInterface
from master.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from master.adapters.sse.event_bus import EventBus
from master.application.services.auth_service import AuthService
from master.application.services.artifact_service import ArtifactService
from master.application.services.device_service import DeviceService
from master.application.services.node_service import NodeService
from master.application.services.project_member_service import ProjectMemberService
from master.application.services.project_node_binding_service import ProjectNodeBindingService
from master.application.services.project_service import ProjectService
from master.application.services.script_download_service import ScriptDownloadService
from master.application.services.plugin_download_service import PluginDownloadService
from master.application.services.script_service import ScriptService
from master.application.services.script_storage_service import ScriptStorageService
from master.application.services.task_service import TaskService
from master.application.services.test_task_service import TestTaskService
from master.application.services.run_trigger_service import RunTriggerService
from master.application.services.run_retry_service import RunRetryService
from master.application.services.run_projection_service import RunProjectionService
from master.plugins.registry import PluginRegistry
from master.plugins.manager import PluginManager
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


def get_plugin_registry(
    container: Annotated[Container, Depends(get_container)],
) -> PluginRegistry:
    """获取已加载的受信任任务类型插件注册表。"""
    return container.plugin_registry()

def get_plugin_manager(container: Annotated[Container, Depends(get_container)]) -> PluginManager:
    return container.plugin_manager()


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


def get_auth_service(
    container: Annotated[Container, Depends(get_container)],
) -> AuthService:
    """从容器解析认证服务。"""
    return container.auth_service()


def get_task_service(
    container: Annotated[Container, Depends(get_container)],
) -> TaskService:
    """从容器解析任务服务。"""
    return container.task_service()


def get_run_trigger_service(
    container: Annotated[Container, Depends(get_container)],
) -> RunTriggerService:
    """从容器解析 Run 触发服务（P6.4）。"""
    return container.run_trigger_service()


def get_run_projection_service(
    container: Annotated[Container, Depends(get_container)],
) -> RunProjectionService:
    """从容器解析 Run 投影服务（P6.4）。"""
    return container.run_projection_service()


def get_run_retry_service(
    container: Annotated[Container, Depends(get_container)],
) -> RunRetryService:
    """从容器解析 Run 重试服务（P6.7）。"""
    return container.run_retry_service()


def get_artifact_service(
    container: Annotated[Container, Depends(get_container)],
) -> ArtifactService:
    """从容器解析产物服务（P6.6）。"""
    return container.artifact_service()


def get_device_service(
    container: Annotated[Container, Depends(get_container)],
) -> DeviceService:
    """从容器解析设备服务。"""
    return container.device_service()


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


def get_script_download_service(
    container: Annotated[Container, Depends(get_container)],
) -> ScriptDownloadService:
    """从容器解析脚本签名下载服务（P4.7）。"""
    return container.script_download_service()


def get_plugin_download_service(
    container: Annotated[Container, Depends(get_container)],
) -> PluginDownloadService:
    """从容器解析插件签名下载服务（P5.5）。"""
    return container.plugin_download_service()


def get_script_storage_service(
    container: Annotated[Container, Depends(get_container)],
) -> ScriptStorageService:
    """从容器解析脚本文件存储服务（P4.7）。"""
    return container.script_storage_service()


def get_script_service(
    container: Annotated[Container, Depends(get_container)],
) -> ScriptService:
    """从容器解析脚本上传/解析服务（P7.3）。"""
    return container.script_service()


def get_test_task_service(
    container: Annotated[Container, Depends(get_container)],
) -> TestTaskService:
    """从容器解析测试任务定义服务（P4.5/P7.4）。"""
    return container.test_task_service()


def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer)
    ],
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
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

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
UowFactoryDep = Annotated[
    SqlAlchemyUnitOfWorkFactory, Depends(get_uow_factory)
]
EventBusDep = Annotated[EventBus, Depends(get_event_bus)]
AuthDep = Annotated[AuthService, Depends(get_auth_service)]
TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
RunTriggerServiceDep = Annotated[
    RunTriggerService, Depends(get_run_trigger_service)
]
RunProjectionServiceDep = Annotated[
    RunProjectionService, Depends(get_run_projection_service)
]
PluginRegistryDep = Annotated[PluginRegistry, Depends(get_plugin_registry)]
PluginManagerDep = Annotated[PluginManager, Depends(get_plugin_manager)]
RunRetryServiceDep = Annotated[RunRetryService, Depends(get_run_retry_service)]
ArtifactServiceDep = Annotated[ArtifactService, Depends(get_artifact_service)]
DeviceServiceDep = Annotated[DeviceService, Depends(get_device_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
ProjectMemberServiceDep = Annotated[
    ProjectMemberService, Depends(get_project_member_service)
]
ProjectNodeBindingServiceDep = Annotated[
    ProjectNodeBindingService, Depends(get_project_node_binding_service)
]
NodeServiceDep = Annotated[NodeService, Depends(get_node_service)]
ScriptDownloadServiceDep = Annotated[
    ScriptDownloadService, Depends(get_script_download_service)
]
PluginDownloadServiceDep = Annotated[
    PluginDownloadService, Depends(get_plugin_download_service)
]
ScriptStorageServiceDep = Annotated[
    ScriptStorageService, Depends(get_script_storage_service)
]
ScriptServiceDep = Annotated[ScriptService, Depends(get_script_service)]
TestTaskServiceDep = Annotated[TestTaskService, Depends(get_test_task_service)]
