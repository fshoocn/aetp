"""CI/CD 集成服务（P8.3，§8.8）。

管理项目 CI/CD 集成、触发绑定和 webhook 处理：
1. 集成 CRUD（项目 owner 管理）
2. 触发绑定 CRUD（maintainer 管理）
3. Webhook 入口（签名验证、delivery 去重、任务触发）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from master.application.errors import TaskNotFoundError
from master.application.services.run_trigger_service import RunTriggerService
from master.domain.enums import TriggerType
from master.domain.models.ci_integration import (
    CiTriggerBinding,
    CiWebhookDelivery,
    ProjectIntegration,
)
from master.domain.repositories import UnitOfWork
from master.domain.time import utcnow

logger = logging.getLogger(__name__)

VALID_PROVIDERS = frozenset({"github", "gitlab", "jenkins", "azure_devops", "generic"})


@dataclass(frozen=True)
class WebhookResult:
    """Webhook 处理结果。"""

    status: str
    delivery_id: str
    triggered_run_ids: tuple[str, ...] = ()
    error: str | None = None


class CiIntegrationService:
    """CI/CD 集成 CRUD 与 webhook 处理。"""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        trigger_service: RunTriggerService | None = None,
        signing_secret: str = "",
    ) -> None:
        self._uow_factory = uow_factory
        self._trigger = trigger_service
        self._signing_secret = signing_secret

    # -- 集成 CRUD -----------------------------------------------------------

    def create_integration(
        self,
        *,
        project_id: str,
        provider: str,
        name: str,
        secret_value: str | None = None,
        config_json: dict | None = None,
        enabled: bool = True,
        created_by: int,
    ) -> ProjectIntegration:
        if provider not in VALID_PROVIDERS:
            raise ValueError(f"不支持的 provider: {provider}，可选: {', '.join(sorted(VALID_PROVIDERS))}")
        if not name.strip():
            raise ValueError("集成名称不能为空")

        secret_hash = None
        if secret_value:
            secret_hash = hashlib.sha256(secret_value.encode("utf-8")).hexdigest()

        with self._uow_factory() as uow:
            integration = ProjectIntegration(
                integration_id=f"CI-{uuid.uuid4().hex.upper()}",
                project_id=project_id,
                provider=provider,
                name=name.strip(),
                secret_hash=secret_hash,
                config_json=config_json or {},
                enabled=enabled,
                created_by=created_by,
            )
            integration = uow.project_integrations.add(integration)

        logger.info("CI 集成已创建: integration_id=%s provider=%s", integration.integration_id, provider)
        return integration

    def list_integrations(
        self, project_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[ProjectIntegration]:
        with self._uow_factory() as uow:
            return uow.project_integrations.list_by_project(project_id, limit=limit, offset=offset)

    def get_integration(
        self, integration_id: str, project_id: str
    ) -> ProjectIntegration | None:
        with self._uow_factory() as uow:
            integration = uow.project_integrations.get_by_integration_id(integration_id)
            if integration is None or integration.project_id != project_id:
                return None
            return integration

    def update_integration(
        self,
        integration_id: str,
        *,
        project_id: str,
        name: str | None = None,
        secret_value: str | None = None,
        config_json: dict | None = None,
        enabled: bool | None = None,
    ) -> ProjectIntegration:
        with self._uow_factory() as uow:
            integration = uow.project_integrations.get_by_integration_id(integration_id)
            if integration is None or integration.project_id != project_id:
                raise ValueError(f"集成不存在: {integration_id}")
            if name is not None:
                integration.name = name.strip()
            if secret_value is not None:
                integration.secret_hash = hashlib.sha256(secret_value.encode("utf-8")).hexdigest()
            if config_json is not None:
                integration.config_json = config_json
            if enabled is not None:
                integration.enabled = enabled
            return uow.project_integrations.update(integration)

    def delete_integration(self, integration_id: str, project_id: str) -> None:
        with self._uow_factory() as uow:
            integration = uow.project_integrations.get_by_integration_id(integration_id)
            if integration is None or integration.project_id != project_id:
                raise ValueError(f"集成不存在: {integration_id}")
            uow.project_integrations.delete(integration_id)
        logger.info("CI 集成已删除: integration_id=%s", integration_id)

    # -- 触发绑定 CRUD -------------------------------------------------------

    def create_binding(
        self,
        *,
        integration_id: str,
        task_id: str,
        event_filter_json: dict | None = None,
        parameter_mapping_json: dict | None = None,
    ) -> CiTriggerBinding:
        with self._uow_factory() as uow:
            integration = uow.project_integrations.get_by_integration_id(integration_id)
            if integration is None:
                raise ValueError(f"集成不存在: {integration_id}")
            task = uow.test_tasks.get_by_task_id(task_id, integration.project_id)
            if task is None:
                raise TaskNotFoundError(f"任务定义不存在: {task_id}")

            binding = CiTriggerBinding(
                binding_id=f"TB-{uuid.uuid4().hex.upper()}",
                integration_id=integration_id,
                task_id=task_id,
                event_filter_json=event_filter_json or {},
                parameter_mapping_json=parameter_mapping_json or {},
                enabled=True,
            )
            return uow.ci_trigger_bindings.add(binding)

    def list_bindings(self, integration_id: str) -> list[CiTriggerBinding]:
        with self._uow_factory() as uow:
            return uow.ci_trigger_bindings.list_by_integration(integration_id)

    def update_binding(
        self,
        binding_id: str,
        *,
        event_filter_json: dict | None = None,
        parameter_mapping_json: dict | None = None,
        enabled: bool | None = None,
    ) -> CiTriggerBinding:
        with self._uow_factory() as uow:
            binding = uow.ci_trigger_bindings.get_by_binding_id(binding_id)
            if binding is None:
                raise ValueError(f"触发绑定不存在: {binding_id}")
            if event_filter_json is not None:
                binding.event_filter_json = event_filter_json
            if parameter_mapping_json is not None:
                binding.parameter_mapping_json = parameter_mapping_json
            if enabled is not None:
                binding.enabled = enabled
            return uow.ci_trigger_bindings.update(binding)

    def delete_binding(self, binding_id: str) -> None:
        with self._uow_factory() as uow:
            binding = uow.ci_trigger_bindings.get_by_binding_id(binding_id)
            if binding is None:
                raise ValueError(f"触发绑定不存在: {binding_id}")
            uow.ci_trigger_bindings.delete(binding_id)

    # -- Webhook 处理 --------------------------------------------------------

    def handle_webhook(
        self,
        integration_id: str,
        *,
        delivery_id: str,
        signature: str,
        payload_body: bytes,
        payload_json: dict,
        headers: dict[str, str] | None = None,
    ) -> WebhookResult:
        """处理 CI/CD webhook（§8.8）。

        1. 查找集成并确定 project_id
        2. 验证签名
        3. 按 (integration_id, delivery_id) 去重
        4. 匹配触发绑定
        5. 创建 Run
        """
        payload_hash = hashlib.sha256(payload_body).hexdigest()

        with self._uow_factory() as uow:
            integration = uow.project_integrations.get_by_integration_id(integration_id)
            if integration is None:
                raise ValueError(f"集成不存在: {integration_id}")
            if not integration.enabled:
                raise ValueError("集成已禁用")

            project_id = integration.project_id

            # 3. 去重
            existing = uow.ci_webhook_deliveries.get_by_integration_delivery(
                integration_id, delivery_id
            )
            if existing is not None:
                logger.info(
                    "Webhook 投递已处理（幂等返回）: integration=%s delivery=%s",
                    integration_id, delivery_id,
                )
                return WebhookResult(
                    status="already_processed",
                    delivery_id=delivery_id,
                    triggered_run_ids=tuple(existing.triggered_run_ids),
                    error=existing.error_message,
                )

            # 2. 验证签名
            if not self._verify_signature(
                integration.secret_hash, signature, payload_body, headers
            ):
                delivery = uow.ci_webhook_deliveries.add(
                    CiWebhookDelivery(
                        integration_id=integration_id,
                        delivery_id=delivery_id,
                        received_at=utcnow(),
                        payload_hash=payload_hash,
                        status="rejected",
                        error_message="签名验证失败",
                    )
                )
                raise ValueError("签名验证失败")

            # 4. 查找匹配的触发绑定
            bindings = uow.ci_trigger_bindings.list_by_integration(integration_id)
            active_bindings = [b for b in bindings if b.enabled]

            triggered_run_ids: list[str] = []
            errors: list[str] = []

            for binding in active_bindings:
                if not self._matches_filter(binding.event_filter_json, payload_json):
                    continue
                # 5. 创建 Run
                try:
                    run_id = self._trigger_run(
                        project_id=project_id,
                        task_id=binding.task_id,
                        integration_id=integration_id,
                        delivery_id=delivery_id,
                        event_filter=binding.event_filter_json,
                        parameter_mapping=binding.parameter_mapping_json,
                        payload_json=payload_json,
                    )
                    triggered_run_ids.append(run_id)
                except Exception as exc:
                    errors.append(f"{binding.task_id}: {exc}")

            status = "accepted" if not errors else ("partial" if triggered_run_ids else "error")
            error_message = "; ".join(errors) if errors else None

            # 写投递记录
            uow.ci_webhook_deliveries.add(
                CiWebhookDelivery(
                    integration_id=integration_id,
                    delivery_id=delivery_id,
                    received_at=utcnow(),
                    payload_hash=payload_hash,
                    status=status,
                    triggered_run_ids=triggered_run_ids,
                    error_message=error_message,
                )
            )

        logger.info(
            "Webhook 处理完成: integration=%s delivery=%s runs=%d status=%s",
            integration_id, delivery_id, len(triggered_run_ids), status,
        )
        return WebhookResult(
            status=status,
            delivery_id=delivery_id,
            triggered_run_ids=tuple(triggered_run_ids),
            error=error_message,
        )

    def list_deliveries(
        self, integration_id: str, *, limit: int = 100
    ) -> list[CiWebhookDelivery]:
        """查询集成的投递记录。"""
        with self._uow_factory() as uow:
            return list(
                reversed(
                    sorted(
                        [
                            d
                            for d in [
                                uow.ci_webhook_deliveries.get_by_integration_delivery(
                                    integration_id, str(i)
                                )
                                for i in range(limit * 2)
                            ]
                            if d is not None
                        ],
                        key=lambda d: d.received_at or datetime.min,
                    )
                )
            )

    def _verify_signature(
        self,
        secret_hash: str | None,
        signature: str,
        payload_body: bytes,
        headers: dict[str, str] | None,
    ) -> bool:
        """验证 webhook 签名。"""
        if not secret_hash:
            return True  # 无密钥则跳过验证
        if not signature:
            return False

        # 支持 sha256=<hex> 格式
        if signature.startswith("sha256="):
            expected_sig = signature[7:]
            computed = hmac.new(
                secret_hash.encode("utf-8"), payload_body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected_sig, computed)

        # 支持原始 hex 格式
        computed = hmac.new(
            secret_hash.encode("utf-8"), payload_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, computed)

    @staticmethod
    def _matches_filter(event_filter: dict, payload: dict) -> bool:
        """检查 payload 是否匹配事件过滤器。"""
        if not event_filter:
            return True

        for key, expected in event_filter.items():
            actual = payload.get(key)
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def _trigger_run(
        self,
        *,
        project_id: str,
        task_id: str,
        integration_id: str,
        delivery_id: str,
        event_filter: dict,
        parameter_mapping: dict,
        payload_json: dict,
    ) -> str:
        """触发 Run（调用 RunTriggerService）。"""
        if self._trigger is None:
            raise ValueError("RunTriggerService 未配置")

        import asyncio

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                self._trigger.trigger(
                    task_id,
                    project_id=project_id,
                    trigger_type=TriggerType.CI_WEBHOOK,
                    trigger_context={
                        "integration_id": integration_id,
                        "delivery_id": delivery_id,
                        "event_filter": event_filter,
                        "parameter_mapping": parameter_mapping,
                        "provider_payload_keys": list(payload_json.keys()),
                    },
                )
            )
            return result.run_id
        finally:
            loop.close()
