"""领域仓储接口（Port）。

仓储接口只依赖领域对象，不依赖任何 ORM / 数据库实现；
具体实现位于 adapters/sqlalchemy/repositories/。

服务层通过 UnitOfWork 访问仓储，保证一次业务操作内
多个仓储共享同一事务。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from master.domain.models import (
    Device,
    Node,
    Project,
    ProjectMember,
    ProjectMemberWithUser,
    ProjectNodeBinding,
    ProjectNodeBindingView,
    RefreshToken,
    ScriptCase,
    Task,
    TaskLog,
    TestScript,
    User,
)


class UserRepository(ABC):
    @abstractmethod
    def get_by_id(self, user_id: int) -> User | None: ...

    @abstractmethod
    def get_by_username(self, username: str) -> User | None: ...

    @abstractmethod
    def list(
        self, *, account_status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[User]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def add(self, user: User) -> User: ...

    @abstractmethod
    def update(self, user: User) -> User: ...


class RefreshTokenRepository(ABC):
    @abstractmethod
    def get_by_hash(self, token_hash: str) -> RefreshToken | None: ...

    @abstractmethod
    def add(self, token: RefreshToken) -> RefreshToken: ...

    @abstractmethod
    def update(self, token: RefreshToken) -> RefreshToken: ...

    @abstractmethod
    def revoke_all_for_user(self, user_id: int) -> int: ...


class TestScriptRepository(ABC):
    @abstractmethod
    def get_by_script_id(self, script_id: str) -> TestScript | None: ...

    @abstractmethod
    def get_by_hash(self, sha256: str) -> TestScript | None: ...

    @abstractmethod
    def find_by_name_version(
        self, project_id: str, name: str, version: int
    ) -> TestScript | None: ...

    @abstractmethod
    def list_by_project(
        self, project_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[TestScript]: ...

    @abstractmethod
    def add(self, script: TestScript) -> TestScript: ...

    @abstractmethod
    def update(self, script: TestScript) -> TestScript: ...


class ScriptCaseRepository(ABC):
    @abstractmethod
    def list_by_script(
        self, script_id: str, *, include_deleted: bool = False
    ) -> list[ScriptCase]: ...

    @abstractmethod
    def get_by_stable_key(
        self, script_id: str, stable_key: str
    ) -> ScriptCase | None: ...

    @abstractmethod
    def add(self, case: ScriptCase) -> ScriptCase: ...

    @abstractmethod
    def add_many(self, cases: list[ScriptCase]) -> list[ScriptCase]: ...

    @abstractmethod
    def update(self, case: ScriptCase) -> ScriptCase: ...


class ProjectRepository(ABC):
    @abstractmethod
    def get_by_project_id(self, project_id: str) -> Project | None: ...

    @abstractmethod
    def get_by_key(self, project_key: str) -> Project | None: ...

    @abstractmethod
    def list_all(self, *, limit: int = 100, offset: int = 0) -> list[Project]: ...

    @abstractmethod
    def list_visible_to_user(
        self, user_id: int, *, limit: int = 100, offset: int = 0
    ) -> list[Project]: ...

    @abstractmethod
    def add(self, project: Project) -> Project: ...

    @abstractmethod
    def update(self, project: Project) -> Project: ...


class ProjectMemberRepository(ABC):
    @abstractmethod
    def get_role(self, project_id: str, user_id: int) -> str | None: ...

    @abstractmethod
    def list_with_users(self, project_id: str) -> list[ProjectMemberWithUser]: ...

    @abstractmethod
    def get_by_project_and_user(
        self, project_id: str, user_id: int
    ) -> ProjectMember | None: ...

    @abstractmethod
    def count_owners(self, project_id: str) -> int: ...

    @abstractmethod
    def add(self, member: ProjectMember) -> ProjectMember: ...

    @abstractmethod
    def update(self, member: ProjectMember) -> ProjectMember: ...

    @abstractmethod
    def remove(self, member: ProjectMember) -> None: ...


class NodeRepository(ABC):
    @abstractmethod
    def list_all(
        self, *, online: bool | None = None, enabled: bool | None = None
    ) -> list[Node]: ...

    @abstractmethod
    def get_by_id(self, node_id: str) -> Node | None: ...


class DeviceRepository(ABC):
    @abstractmethod
    def list_all(self, *, online: bool | None = None) -> list[Device]: ...

    @abstractmethod
    def get_by_id(self, device_id: str) -> Device | None: ...

    @abstractmethod
    def list_for_project(
        self, project_id: str, *, online: bool | None = None
    ) -> list[Device]: ...

    @abstractmethod
    def get_for_project(self, project_id: str, device_id: str) -> Device | None: ...


class ProjectNodeBindingRepository(ABC):
    @abstractmethod
    def list_with_nodes(self, project_id: str) -> list[ProjectNodeBindingView]: ...

    @abstractmethod
    def get(self, project_id: str, node_id: str) -> ProjectNodeBinding | None: ...

    @abstractmethod
    def add(self, binding: ProjectNodeBinding) -> ProjectNodeBinding: ...

    @abstractmethod
    def update(self, binding: ProjectNodeBinding) -> ProjectNodeBinding: ...

    @abstractmethod
    def remove(self, binding: ProjectNodeBinding) -> None: ...


class TaskRepository(ABC):
    @abstractmethod
    def add(self, task: Task) -> Task: ...

    @abstractmethod
    def get_by_task_id(self, task_id: str, project_id: str | None = None) -> Task | None: ...

    @abstractmethod
    def list(
        self,
        *,
        project_id: str | None = None,
        device_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Task]: ...

    @abstractmethod
    def update(self, task: Task) -> Task: ...


class TaskLogRepository(ABC):
    @abstractmethod
    def list_by_task(
        self, task_id: str, project_id: str | None = None
    ) -> list[TaskLog]: ...

    @abstractmethod
    def add_many(self, logs: list[TaskLog]) -> list[TaskLog]: ...


class UnitOfWork(ABC):
    """工作单元：一个业务事务内共享同一数据库会话。

    仓储通过属性访问（如 uow.users / uow.tasks）。
    使用方式：with uow() as uow: ... （正常提交，异常回滚）
    """

    users: UserRepository
    refresh_tokens: RefreshTokenRepository
    test_scripts: TestScriptRepository
    script_cases: ScriptCaseRepository
    projects: ProjectRepository
    members: ProjectMemberRepository
    nodes: NodeRepository
    devices: DeviceRepository
    bindings: ProjectNodeBindingRepository
    tasks: TaskRepository
    task_logs: TaskLogRepository

    @abstractmethod
    def __enter__(self) -> "UnitOfWork": ...

    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...
