"""SQLAlchemy 测试任务定义仓储实现（P3.3）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import TestScript as TestScriptORM
from master.adapters.sqlalchemy.orm import TestTask as TestTaskORM
from master.domain.models import TestTask
from master.domain.repositories import TestTaskRepository


def _to_domain(orm: TestTaskORM) -> TestTask:
    return TestTask(
        id=orm.id,
        task_id=orm.task_id,
        project_id=orm.project.project_id if orm.project is not None else "",
        script_id=orm.script.script_id if orm.script is not None else "",
        script_version=orm.script.version if orm.script is not None else 0,
        task_type=orm.task_type,
        name=orm.name,
        default_case_selection=list(orm.default_case_selection or []),
        node_ids=list(orm.node_ids or []),
        split_policy=dict(orm.split_policy or {}),
        retry_policy=dict(orm.retry_policy or {}),
        timeout_s=orm.timeout_s,
        enabled=orm.enabled,
        priority=orm.priority,
        created_by=orm.created_by,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class TestTaskRepositoryImpl(TestTaskRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_task_id(
        self, task_id: str, project_id: str | None = None
    ) -> TestTask | None:
        stmt = (
            select(TestTaskORM)
            .options(
                joinedload(TestTaskORM.project), joinedload(TestTaskORM.script)
            )
            .where(TestTaskORM.task_id == task_id)
        )
        if project_id is not None:
            stmt = stmt.where(
                TestTaskORM.project_pk
                == select(ProjectORM.id)
                .where(ProjectORM.project_id == project_id)
                .scalar_subquery()
            )
        orm = self._s.execute(stmt).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def find_by_name(self, project_id: str, name: str) -> TestTask | None:
        orm = self._s.execute(
            select(TestTaskORM)
            .options(
                joinedload(TestTaskORM.project), joinedload(TestTaskORM.script)
            )
            .where(
                TestTaskORM.project_pk
                == select(ProjectORM.id)
                .where(ProjectORM.project_id == project_id)
                .scalar_subquery(),
                TestTaskORM.name == name,
            )
        ).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list_by_project(
        self,
        project_id: str,
        *,
        enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TestTask]:
        stmt = (
            select(TestTaskORM)
            .options(
                joinedload(TestTaskORM.project), joinedload(TestTaskORM.script)
            )
            .where(
                TestTaskORM.project_pk
                == select(ProjectORM.id)
                .where(ProjectORM.project_id == project_id)
                .scalar_subquery()
            )
            .order_by(TestTaskORM.created_at.desc(), TestTaskORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if enabled is not None:
            stmt = stmt.where(TestTaskORM.enabled.is_(enabled))
        return [_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, task: TestTask) -> TestTask:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == task.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {task.project_id}")
        script_pk = self._s.execute(
            select(TestScriptORM.id).where(
                TestScriptORM.script_id == task.script_id,
                TestScriptORM.version == task.script_version,
            )
        ).scalar_one_or_none()
        if script_pk is None:
            raise ValueError(
                f"引用的脚本版本不存在: {task.script_id} v{task.script_version}"
            )
        if task.created_by is None:
            raise ValueError("缺少创建者 created_by")
        orm = TestTaskORM(
            task_id=task.task_id,
            project_pk=project_pk,
            script_pk=script_pk,
            task_type=task.task_type,
            name=task.name,
            default_case_selection=task.default_case_selection,
            node_ids=task.node_ids,
            split_policy=task.split_policy,
            retry_policy=task.retry_policy,
            timeout_s=task.timeout_s,
            enabled=task.enabled,
            priority=task.priority,
            created_by=task.created_by,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def update(self, task: TestTask) -> TestTask:
        orm = self._s.get(TestTaskORM, task.id)
        if orm is None:
            raise ValueError(f"任务定义不存在: id={task.id}")
        orm.name = task.name
        orm.task_type = task.task_type
        orm.default_case_selection = task.default_case_selection
        orm.node_ids = task.node_ids
        orm.split_policy = task.split_policy
        orm.retry_policy = task.retry_policy
        orm.timeout_s = task.timeout_s
        orm.enabled = task.enabled
        orm.priority = task.priority
        # script_ref 切换：按 script_id + version 重新解析 script_pk
        if (
            task.script_id
            and orm.script is not None
            and (task.script_id != orm.script.script_id or task.script_version != orm.script.version)
        ):
            script_pk = self._s.execute(
                select(TestScriptORM.id).where(
                    TestScriptORM.script_id == task.script_id,
                    TestScriptORM.version == task.script_version,
                )
            ).scalar_one_or_none()
            if script_pk is None:
                raise ValueError(
                    f"引用的脚本版本不存在: {task.script_id} v{task.script_version}"
                )
            orm.script_pk = script_pk
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)
