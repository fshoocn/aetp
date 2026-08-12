"""SQLAlchemy 工作单元实现。

一个业务事务内共享同一 Session，所有仓储绑定到该 Session，
保证跨仓储操作原子性（正常提交 / 异常回滚）。
"""

from __future__ import annotations

from typing import Any

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
        self._session = None

        # 仓储在 __enter__ 中绑定 session
        self.users: UserRepositoryImpl = None  # type: ignore[assignment]
        self.projects: ProjectRepositoryImpl = None  # type: ignore[assignment]
        self.members: ProjectMemberRepositoryImpl = None  # type: ignore[assignment]
        self.nodes: NodeRepositoryImpl = None  # type: ignore[assignment]
        self.devices: DeviceRepositoryImpl = None  # type: ignore[assignment]
        self.bindings: ProjectNodeBindingRepositoryImpl = None  # type: ignore[assignment]
        self.tasks: TaskRepositoryImpl = None  # type: ignore[assignment]
        self.task_logs: TaskLogRepositoryImpl = None  # type: ignore[assignment]

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        self._session = self._database.session()
        self.users = UserRepositoryImpl(self._session)
        self.projects = ProjectRepositoryImpl(self._session)
        self.members = ProjectMemberRepositoryImpl(self._session)
        self.nodes = NodeRepositoryImpl(self._session)
        self.devices = DeviceRepositoryImpl(self._session)
        self.bindings = ProjectNodeBindingRepositoryImpl(self._session)
        self.tasks = TaskRepositoryImpl(self._session)
        self.task_logs = TaskLogRepositoryImpl(self._session)
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
