"""依赖注入容器（dependency-injector）。

统一管理应用级依赖：
- database:       数据库实例（单例，创建时执行迁移）
- uow_factory:    工作单元工厂（每个业务操作一个事务）
- event_bus:      SSE 事件总线（单例）
- auth_service:   认证服务（依赖 uow_factory）
- task_service:   任务服务
- device_service: 设备服务
- ...

组合根（run.py / main.py lifespan）创建容器并初始化；
各层通过依赖注入获取实例，不自行 new。
"""

from __future__ import annotations

from dependency_injector import containers, providers

from master.config import get_settings
from master.adapters.sqlalchemy.database_interface import DatabaseInterface
from master.adapters.sqlalchemy.database_factory import create_database
from master.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from master.adapters.sse.event_bus import EventBus
from master.application.services.auth_service import AuthService
from master.application.services.device_service import DeviceService
from master.application.services.project_service import ProjectService
from master.application.services.project_member_service import ProjectMemberService
from master.application.services.project_node_binding_service import ProjectNodeBindingService
from master.application.services.node_service import NodeService
from master.application.services.task_service import TaskService


def _init_database(url: str) -> DatabaseInterface:
    """创建数据库实例并完成建表 + 迁移。"""
    db = create_database(url)
    db.connect()
    return db


def _build_uow_factory(database: DatabaseInterface):
    """返回创建新工作单元实例的工厂函数（每次业务操作一个 UoW）。"""

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(database)

    return factory


class Container(containers.DeclarativeContainer):
    """AETP Master 应用容器。"""

    # 惰性读取进程级配置（组合根 configure() 之后才求值）
    database_url = providers.Callable(lambda: get_settings().database_url)

    # 数据库：进程级单例
    database = providers.Singleton(_init_database, database_url)

    # 工作单元：每次解析创建新实例，绑定同一 Session 与全部仓储
    uow_factory = providers.Callable(_build_uow_factory, database=database)

    # SSE 事件总线：进程级单例
    event_bus = providers.Singleton(EventBus)

    # 认证服务
    auth_service = providers.Factory(AuthService, uow_factory=uow_factory)

    # 任务服务
    task_service = providers.Factory(TaskService, uow_factory=uow_factory)

    # 设备服务
    device_service = providers.Factory(DeviceService, uow_factory=uow_factory)

    # 项目服务
    project_service = providers.Factory(ProjectService, uow_factory=uow_factory)

    # 项目成员授权服务
    project_member_service = providers.Factory(
        ProjectMemberService, uow_factory=uow_factory
    )

    # 项目节点绑定服务
    project_node_binding_service = providers.Factory(
        ProjectNodeBindingService, uow_factory=uow_factory
    )

    # Node/Device 平台资产只读查询服务
    node_service = providers.Factory(NodeService, uow_factory=uow_factory)
