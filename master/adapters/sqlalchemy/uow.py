"""SQLAlchemy 工作单元实现。

一个业务事务内共享同一 Session，所有仓储绑定到该 Session，
保证跨仓储操作原子性（正常提交 / 异常回滚）。

仓储属性类型以基类 UnitOfWork 的接口注解为准（依赖倒置），
不在本类中重声明；具体实现统一在 __enter__ 中绑定 Session 后创建。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from master.adapters.sqlalchemy.database_interface import DatabaseInterface
from master.adapters.sqlalchemy.repositories import (
    DeviceRepositoryImpl,
    NodeRepositoryImpl,
    ProjectMemberRepositoryImpl,
    ProjectNodeBindingRepositoryImpl,
    ProjectRepositoryImpl,
    TaskLogRepositoryImpl,
    TaskRepositoryImpl,
    UserRepositoryImpl,
)
from master.domain.repositories import UnitOfWork


class SqlAlchemyUnitOfWork(UnitOfWork):
    def __init__(self, database: DatabaseInterface) -> None:
        self._database = database
        self._session: Session | None = None

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        session = self._database.session()
        self._session = session
        self.users = UserRepositoryImpl(session)
        self.projects = ProjectRepositoryImpl(session)
        self.members = ProjectMemberRepositoryImpl(session)
        self.nodes = NodeRepositoryImpl(session)
        self.devices = DeviceRepositoryImpl(session)
        self.bindings = ProjectNodeBindingRepositoryImpl(session)
        self.tasks = TaskRepositoryImpl(session)
        self.task_logs = TaskLogRepositoryImpl(session)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._session is None:
            return
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None
