"""TaskRun 仓储实现。"""

from __future__ import annotations

from aetp_protocol.task import RunSnapshot
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import TaskRun as TaskRunORM
from master.adapters.sqlalchemy.orm import User as UserORM
from master.domain.enums import RunStatus, TriggerType
from master.domain.models import TaskRun
from master.domain.repositories import TaskRunRepository


def _to_domain(orm: TaskRunORM) -> TaskRun:
    return TaskRun(
        id=orm.id,
        run_id=orm.run_id,
        project_id=orm.project.project_id if orm.project is not None else "",
        task_id=orm.task_id or "",
        task_revision=orm.task_revision,
        script_ref=dict(orm.script_ref or {}),
        case_selection=list(orm.case_selection or []),
        split_policy=dict(orm.split_policy or {}),
        snapshot=RunSnapshot.model_validate(orm.task_snapshot) if orm.task_snapshot is not None else None,
        trigger_type=TriggerType(orm.trigger_type),
        triggered_by_user_id=orm.triggered_by_user_pk,
        integration_id=orm.integration_id,
        trigger_context=dict(orm.trigger_context) if orm.trigger_context else None,
        status=RunStatus(orm.status),
        started_at=orm.started_at,
        finished_at=orm.finished_at,
        log_complete=orm.log_complete,
        last_log_sequence=orm.last_log_sequence,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class TaskRunRepositoryImpl(TaskRunRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    @staticmethod
    def _options():
        return [joinedload(TaskRunORM.project)]

    def add(self, run: TaskRun) -> TaskRun:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == run.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"Project not found: {run.project_id}")
        if run.snapshot is None:
            raise ValueError(f"Task snapshot missing: {run.task_id}")

        triggered_by_user_pk = None
        if run.triggered_by_user_id is not None:
            triggered_by_user_pk = self._s.execute(
                select(UserORM.id).where(UserORM.id == run.triggered_by_user_id)
            ).scalar_one_or_none()
            if triggered_by_user_pk is None:
                raise ValueError(f"User not found: {run.triggered_by_user_id}")

        orm = TaskRunORM(
            run_id=run.run_id,
            task_id=run.task_id,
            project_pk=project_pk,
            task_revision=run.task_revision,
            script_ref=run.script_ref,
            case_selection=run.case_selection,
            split_policy=run.split_policy,
            task_snapshot=run.snapshot.model_dump(mode="json"),
            trigger_type=run.trigger_type.value,
            triggered_by_user_pk=triggered_by_user_pk,
            integration_id=run.integration_id,
            trigger_context=run.trigger_context,
            status=run.status.value,
            started_at=run.started_at,
            finished_at=run.finished_at,
            log_complete=run.log_complete,
            last_log_sequence=run.last_log_sequence,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def get_by_run_id(self, run_id: str, project_id: str | None = None) -> TaskRun | None:
        statement = (
            select(TaskRunORM)
            .options(*self._options())
            .where(TaskRunORM.run_id == run_id)
        )
        if project_id is not None:
            statement = statement.where(
                TaskRunORM.project_pk
                == select(ProjectORM.id).where(ProjectORM.project_id == project_id).scalar_subquery()
            )
        orm = self._s.execute(statement).scalars().one_or_none()
        return _to_domain(orm) if orm is not None else None

    def list(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        trigger_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskRun]:
        statement = (
            select(TaskRunORM)
            .options(*self._options())
            .order_by(TaskRunORM.created_at.desc(), TaskRunORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if project_id is not None:
            statement = statement.where(
                TaskRunORM.project_pk
                == select(ProjectORM.id).where(ProjectORM.project_id == project_id).scalar_subquery()
            )
        if task_id is not None:
            statement = statement.where(TaskRunORM.task_id == task_id)
        if status is not None:
            statement = statement.where(TaskRunORM.status == status)
        if trigger_type is not None:
            statement = statement.where(TaskRunORM.trigger_type == trigger_type)
        return [_to_domain(orm) for orm in self._s.execute(statement).scalars().all()]

    def update(self, run: TaskRun) -> TaskRun:
        orm = self._s.get(TaskRunORM, run.id)
        if orm is None:
            raise ValueError(f"Run 不存在: id={run.id}")
        orm.status = run.status.value
        orm.started_at = run.started_at
        orm.finished_at = run.finished_at
        orm.integration_id = run.integration_id
        orm.trigger_context = run.trigger_context
        orm.log_complete = run.log_complete
        orm.last_log_sequence = run.last_log_sequence
        orm.task_id = run.task_id
        orm.task_revision = run.task_revision
        if run.snapshot is None:
            raise ValueError(f"Task snapshot missing: {run.task_id}")
        orm.task_snapshot = run.snapshot.model_dump(mode="json")
        self._s.flush()
        self._s.refresh(orm)
        return _to_domain(orm)

    def list_non_terminal(self, limit: int = 1000) -> list[TaskRun]:
        statement = (
            select(TaskRunORM)
            .options(*self._options())
            .where(
                TaskRunORM.status.in_(
                    [
                        RunStatus.CREATED.value,
                        RunStatus.DISPATCHED.value,
                        RunStatus.ACKED.value,
                        RunStatus.RUNNING.value,
                    ]
                )
            )
            .order_by(TaskRunORM.id)
            .limit(limit)
        )
        return [_to_domain(orm) for orm in self._s.execute(statement).scalars().all()]
