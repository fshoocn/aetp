"""SQLAlchemy CI/CD 集成仓储实现。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from master.adapters.sqlalchemy.orm import Project as ProjectORM
from master.adapters.sqlalchemy.orm import ProjectIntegration as IntegrationORM
from master.adapters.sqlalchemy.orm import CiTriggerBinding as BindingORM
from master.adapters.sqlalchemy.orm import CiWebhookDelivery as DeliveryORM
from master.adapters.sqlalchemy.orm import TestTask as TaskORM
from master.domain.models.ci_integration import (
    CiTriggerBinding,
    CiWebhookDelivery,
    ProjectIntegration,
)
from master.domain.repositories import (
    CiTriggerBindingRepository,
    CiWebhookDeliveryRepository,
    ProjectIntegrationRepository,
)


def _integration_to_domain(orm: IntegrationORM) -> ProjectIntegration:
    return ProjectIntegration(
        id=orm.id,
        integration_id=orm.integration_id,
        project_id=orm.project.project_id if orm.project is not None else "",
        provider=orm.provider,
        name=orm.name,
        secret_hash=orm.secret_hash,
        config_json=dict(orm.config_json or {}),
        enabled=orm.enabled,
        created_by=orm.created_by,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _binding_to_domain(orm: BindingORM) -> CiTriggerBinding:
    return CiTriggerBinding(
        id=orm.id,
        binding_id=orm.binding_id,
        integration_id=orm.integration.integration_id if orm.integration is not None else "",
        task_id=orm.task.task_id if orm.task is not None else "",
        event_filter_json=dict(orm.event_filter_json or {}),
        parameter_mapping_json=dict(orm.parameter_mapping_json or {}),
        enabled=orm.enabled,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def _delivery_to_domain(orm: DeliveryORM) -> CiWebhookDelivery:
    return CiWebhookDelivery(
        id=orm.id,
        integration_id=orm.integration.integration_id if orm.integration is not None else "",
        delivery_id=orm.delivery_id,
        received_at=orm.received_at,
        payload_hash=orm.payload_hash,
        status=orm.status,
        triggered_run_ids=list(orm.triggered_run_ids or []),
        error_message=orm.error_message,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


class ProjectIntegrationRepositoryImpl(ProjectIntegrationRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_integration_id(self, integration_id: str) -> ProjectIntegration | None:
        orm = self._s.execute(
            select(IntegrationORM)
            .options(joinedload(IntegrationORM.project))
            .where(IntegrationORM.integration_id == integration_id)
        ).scalars().one_or_none()
        return _integration_to_domain(orm) if orm is not None else None

    def list_by_project(
        self, project_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[ProjectIntegration]:
        stmt = (
            select(IntegrationORM)
            .options(joinedload(IntegrationORM.project))
            .where(
                IntegrationORM.project_pk
                == select(ProjectORM.id)
                .where(ProjectORM.project_id == project_id)
                .scalar_subquery()
            )
            .order_by(IntegrationORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_integration_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, integration: ProjectIntegration) -> ProjectIntegration:
        project_pk = self._s.execute(
            select(ProjectORM.id).where(ProjectORM.project_id == integration.project_id)
        ).scalar_one_or_none()
        if project_pk is None:
            raise ValueError(f"项目不存在: {integration.project_id}")
        orm = IntegrationORM(
            integration_id=integration.integration_id,
            project_pk=project_pk,
            provider=integration.provider,
            name=integration.name,
            secret_hash=integration.secret_hash,
            config_json=integration.config_json,
            enabled=integration.enabled,
            created_by=integration.created_by or 0,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _integration_to_domain(orm)

    def update(self, integration: ProjectIntegration) -> ProjectIntegration:
        orm = self._s.get(IntegrationORM, integration.id)
        if orm is None:
            raise ValueError(f"集成不存在: id={integration.id}")
        orm.provider = integration.provider
        orm.name = integration.name
        orm.secret_hash = integration.secret_hash
        orm.config_json = integration.config_json
        orm.enabled = integration.enabled
        self._s.flush()
        self._s.refresh(orm)
        return _integration_to_domain(orm)

    def delete(self, integration_id: str) -> None:
        orm = self._s.execute(
            select(IntegrationORM).where(IntegrationORM.integration_id == integration_id)
        ).scalars().one_or_none()
        if orm is None:
            raise ValueError(f"集成不存在: {integration_id}")
        self._s.delete(orm)
        self._s.flush()


class CiTriggerBindingRepositoryImpl(CiTriggerBindingRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_binding_id(self, binding_id: str) -> CiTriggerBinding | None:
        orm = self._s.execute(
            select(BindingORM)
            .options(joinedload(BindingORM.integration), joinedload(BindingORM.task))
            .where(BindingORM.binding_id == binding_id)
        ).scalars().one_or_none()
        return _binding_to_domain(orm) if orm is not None else None

    def list_by_integration(
        self, integration_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[CiTriggerBinding]:
        stmt = (
            select(BindingORM)
            .options(joinedload(BindingORM.integration), joinedload(BindingORM.task))
            .where(
                BindingORM.integration_pk
                == select(IntegrationORM.id)
                .where(IntegrationORM.integration_id == integration_id)
                .scalar_subquery()
            )
            .order_by(BindingORM.id)
            .limit(limit)
            .offset(offset)
        )
        return [_binding_to_domain(o) for o in self._s.execute(stmt).scalars().all()]

    def add(self, binding: CiTriggerBinding) -> CiTriggerBinding:
        integration_pk = self._s.execute(
            select(IntegrationORM.id).where(IntegrationORM.integration_id == binding.integration_id)
        ).scalar_one_or_none()
        if integration_pk is None:
            raise ValueError(f"集成不存在: {binding.integration_id}")
        task_pk = self._s.execute(
            select(TaskORM.id).where(TaskORM.task_id == binding.task_id)
        ).scalar_one_or_none()
        if task_pk is None:
            raise ValueError(f"任务定义不存在: {binding.task_id}")
        orm = BindingORM(
            binding_id=binding.binding_id,
            integration_pk=integration_pk,
            task_pk=task_pk,
            event_filter_json=binding.event_filter_json,
            parameter_mapping_json=binding.parameter_mapping_json,
            enabled=binding.enabled,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _binding_to_domain(orm)

    def update(self, binding: CiTriggerBinding) -> CiTriggerBinding:
        orm = self._s.get(BindingORM, binding.id)
        if orm is None:
            raise ValueError(f"触发绑定不存在: id={binding.id}")
        orm.event_filter_json = binding.event_filter_json
        orm.parameter_mapping_json = binding.parameter_mapping_json
        orm.enabled = binding.enabled
        self._s.flush()
        self._s.refresh(orm)
        return _binding_to_domain(orm)

    def delete(self, binding_id: str) -> None:
        orm = self._s.execute(
            select(BindingORM).where(BindingORM.binding_id == binding_id)
        ).scalars().one_or_none()
        if orm is None:
            raise ValueError(f"触发绑定不存在: {binding_id}")
        self._s.delete(orm)
        self._s.flush()


class CiWebhookDeliveryRepositoryImpl(CiWebhookDeliveryRepository):
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_integration_delivery(
        self, integration_id: str, delivery_id: str
    ) -> CiWebhookDelivery | None:
        orm = self._s.execute(
            select(DeliveryORM)
            .options(joinedload(DeliveryORM.integration))
            .where(
                DeliveryORM.integration_pk
                == select(IntegrationORM.id)
                .where(IntegrationORM.integration_id == integration_id)
                .scalar_subquery(),
                DeliveryORM.delivery_id == delivery_id,
            )
        ).scalars().one_or_none()
        return _delivery_to_domain(orm) if orm is not None else None

    def add(self, delivery: CiWebhookDelivery) -> CiWebhookDelivery:
        integration_pk = self._s.execute(
            select(IntegrationORM.id).where(IntegrationORM.integration_id == delivery.integration_id)
        ).scalar_one_or_none()
        if integration_pk is None:
            raise ValueError(f"集成不存在: {delivery.integration_id}")
        orm = DeliveryORM(
            integration_pk=integration_pk,
            delivery_id=delivery.delivery_id,
            received_at=delivery.received_at,
            payload_hash=delivery.payload_hash,
            status=delivery.status,
            triggered_run_ids=delivery.triggered_run_ids,
            error_message=delivery.error_message,
        )
        self._s.add(orm)
        self._s.flush()
        self._s.refresh(orm)
        return _delivery_to_domain(orm)

    def update(self, delivery: CiWebhookDelivery) -> CiWebhookDelivery:
        orm = self._s.get(DeliveryORM, delivery.id)
        if orm is None:
            raise ValueError(f"投递记录不存在: id={delivery.id}")
        orm.status = delivery.status
        orm.triggered_run_ids = delivery.triggered_run_ids
        orm.error_message = delivery.error_message
        self._s.flush()
        self._s.refresh(orm)
        return _delivery_to_domain(orm)
