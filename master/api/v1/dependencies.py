"""v1 API 依赖注入：容器、服务和当前用户。"""

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
from master.application.services.artifact_upload_signing_service import ArtifactUploadSigningService
from master.application.services.auth_service import AuthService
from master.application.services.ci_integration_service import CiIntegrationService
from master.application.services.device_service import DeviceService
from master.application.services.event_publisher import EventPublisher
from master.application.services.hook_runner import HookRunner
from master.application.services.node_service import NodeService
from master.application.services.notification_service import NotificationService
from master.application.services.plugin_download_service import PluginDownloadService
from master.application.services.project_member_service import ProjectMemberService
from master.application.services.project_node_binding_service import (
    ProjectNodeBindingService,
)
from master.application.services.project_service import ProjectService
from master.application.services.run_cancel_service import RunCancelService
from master.application.services.run_projection_service import RunProjectionService
from master.application.services.run_retry_service import RunRetryService
from master.application.services.run_trigger_service import RunTriggerService
from master.application.services.schedule_service import ScheduleService
from master.application.services.script_download_service import ScriptDownloadService
from master.application.services.script_service import ScriptService
from master.application.services.script_storage_service import ScriptStorageService
from master.application.services.script_verification_service import (
    ScriptVerificationService,
)
from master.application.services.test_task_service import TestTaskService
from master.application.services.v2_scheduler_service import V2SchedulerService
from master.application.services.v2_task_service import V2TaskService
from master.bootstrap.container import Container
from master.domain.enums import AccountStatus
from master.domain.models import User
from master.domain.repositories import UnitOfWork
from master.plugins.manager import PluginManager
from master.plugins.registry import PluginRegistry

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


def get_run_cancel_service(
    container: Annotated[Container, Depends(get_container)],
) -> RunCancelService:
    """从容器解析 Run 取消服务（P8.1）。"""
    return container.run_cancel_service()


def get_artifact_service(
    container: Annotated[Container, Depends(get_container)],
) -> ArtifactService:
    """从容器解析产物服务（P6.6）。"""
    return container.artifact_service()


def get_artifact_upload_signing_service(
    container: Annotated[Container, Depends(get_container)],
) -> ArtifactUploadSigningService:
    """获取 Agent Artifact 上传 URL 签名服务。"""
    return container.artifact_upload_signing_service()


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


def get_script_verification_service(
    container: Annotated[Container, Depends(get_container)],
) -> ScriptVerificationService:
    """获取 Agent 脚本验证下发服务。"""
    return container.script_verification_service()


def get_test_task_service(
    container: Annotated[Container, Depends(get_container)],
) -> TestTaskService:
    """从容器解析测试任务定义服务（P4.5/P7.4）。"""
    return container.test_task_service()


def get_v2_task_service(
    container: Annotated[Container, Depends(get_container)],
) -> V2TaskService:
    """获取 V2 多脚本任务与 Run Snapshot 服务。"""
    return container.v2_task_service()


def get_v2_scheduler_service(
    container: Annotated[Container, Depends(get_container)],
) -> V2SchedulerService:
    """获取 V2 Snapshot 调度服务。"""
    return container.v2_scheduler_service()


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
RunTriggerServiceDep = Annotated[RunTriggerService, Depends(get_run_trigger_service)]
RunProjectionServiceDep = Annotated[RunProjectionService, Depends(get_run_projection_service)]
PluginRegistryDep = Annotated[PluginRegistry, Depends(get_plugin_registry)]
PluginManagerDep = Annotated[PluginManager, Depends(get_plugin_manager)]
RunRetryServiceDep = Annotated[RunRetryService, Depends(get_run_retry_service)]
RunCancelServiceDep = Annotated[RunCancelService, Depends(get_run_cancel_service)]
ArtifactServiceDep = Annotated[ArtifactService, Depends(get_artifact_service)]
ArtifactUploadSigningServiceDep = Annotated[
    ArtifactUploadSigningService,
    Depends(get_artifact_upload_signing_service),
]
DeviceServiceDep = Annotated[DeviceService, Depends(get_device_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
ProjectMemberServiceDep = Annotated[ProjectMemberService, Depends(get_project_member_service)]
ProjectNodeBindingServiceDep = Annotated[ProjectNodeBindingService, Depends(get_project_node_binding_service)]
NodeServiceDep = Annotated[NodeService, Depends(get_node_service)]
ScriptDownloadServiceDep = Annotated[ScriptDownloadService, Depends(get_script_download_service)]
PluginDownloadServiceDep = Annotated[PluginDownloadService, Depends(get_plugin_download_service)]
ScriptStorageServiceDep = Annotated[ScriptStorageService, Depends(get_script_storage_service)]
ScriptServiceDep = Annotated[ScriptService, Depends(get_script_service)]
ScriptVerificationServiceDep = Annotated[ScriptVerificationService, Depends(get_script_verification_service)]
TestTaskServiceDep = Annotated[TestTaskService, Depends(get_test_task_service)]
V2TaskServiceDep = Annotated[V2TaskService, Depends(get_v2_task_service)]
V2SchedulerServiceDep = Annotated[V2SchedulerService, Depends(get_v2_scheduler_service)]


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


def get_ci_integration_service(
    container: Annotated[Container, Depends(get_container)],
) -> CiIntegrationService:
    """从容器解析 CI/CD 集成服务（P8.3）。"""
    return container.ci_integration_service()


CiIntegrationServiceDep = Annotated[CiIntegrationService, Depends(get_ci_integration_service)]


def get_hook_runner(
    container: Annotated[Container, Depends(get_container)],
) -> HookRunner:
    """从容器解析 Hook 执行器（P8.4）。"""
    return container.hook_runner()


HookRunnerDep = Annotated[HookRunner, Depends(get_hook_runner)]
