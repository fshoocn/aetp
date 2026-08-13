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
    AuditLogRepositoryImpl,
    DeviceRepositoryImpl,
    DomainEventRepositoryImpl,
    InboxMessageRepositoryImpl,
    NodeRepositoryImpl,
    OutboxMessageRepositoryImpl,
    ProjectMemberRepositoryImpl,
    ProjectNodeBindingRepositoryImpl,
    ProjectRepositoryImpl,
    RefreshTokenRepositoryImpl,
    RunArtifactRepositoryImpl,
    RunCaseResultRepositoryImpl,
    RunResultRepositoryImpl,
    RunShardRepositoryImpl,
    ScriptCaseRepositoryImpl,
    ShardAttemptRepositoryImpl,
    TaskLogRepositoryImpl,
    TaskRepositoryImpl,
    TaskRunRepositoryImpl,
    TestScriptRepositoryImpl,
    TestTaskRepositoryImpl,
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
        self.refresh_tokens = RefreshTokenRepositoryImpl(session)
        self.test_scripts = TestScriptRepositoryImpl(session)
        self.script_cases = ScriptCaseRepositoryImpl(session)
        self.test_tasks = TestTaskRepositoryImpl(session)
        self.task_runs = TaskRunRepositoryImpl(session)
        self.run_shards = RunShardRepositoryImpl(session)
        self.shard_attempts = ShardAttemptRepositoryImpl(session)
        self.run_case_results = RunCaseResultRepositoryImpl(session)
        self.run_artifacts = RunArtifactRepositoryImpl(session)
        self.run_results = RunResultRepositoryImpl(session)
        self.inbox_messages = InboxMessageRepositoryImpl(session)
        self.outbox_messages = OutboxMessageRepositoryImpl(session)
        self.domain_events = DomainEventRepositoryImpl(session)
        self.audit_logs = AuditLogRepositoryImpl(session)
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


class SqlAlchemyUnitOfWorkFactory:
    """SqlAlchemyUnitOfWork 的工厂：每次调用返回一个新 UoW（一个新事务）。

    自身无状态（仅持有 database），可在 DI 容器中作为进程级单例注入；
    服务通过 `with self._uow_factory() as uow:` 获得相互独立的事务。
    """

    def __init__(self, database: DatabaseInterface) -> None:
        self._database = database

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._database)
