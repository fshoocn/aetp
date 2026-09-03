"""项目范围通知端点 / 事件订阅 / 投递状态 API（P7.6，§10.5）。

密钥永不回显：API 响应不包含 secret_value；配置中如含 webhook token
等敏感字段由调用方自行脱敏。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, status

from master.api.dependencies import IdempotencyServiceDep, NotificationServiceDep
from master.api.idempotency import complete as complete_idempotency
from master.api.idempotency import release as release_idempotency
from master.api.idempotency import reserve_or_replay
from master.api.permissions import (
    ProjectAccessDep,
    ProjectOperatorDep,
    ProjectOwnerDep,
)
from master.api.schemas import (
    DeliveryOut,
    EndpointCreate,
    EndpointOut,
    EndpointUpdate,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2")

endpoints_router = APIRouter(
    prefix="/projects/{project_id}/notification-endpoints",
    tags=["notifications"],
)

subscriptions_router = APIRouter(
    prefix="/projects/{project_id}/event-subscriptions",
    tags=["notifications"],
)

deliveries_router = APIRouter(
    prefix="/projects/{project_id}/event-deliveries",
    tags=["notifications"],
)


# -- 通知端点 ---------------------------------------------------------------


@endpoints_router.get("", response_model=list[EndpointOut])
def list_endpoints(
    project_id: str,
    _access: ProjectAccessDep,
    service: NotificationServiceDep,
) -> list[EndpointOut]:
    eps = service.list_endpoints(project_id)
    return [EndpointOut.model_validate(ep) for ep in eps]


@endpoints_router.post("", response_model=EndpointOut, status_code=status.HTTP_201_CREATED)
def create_endpoint(
    project_id: str,
    body: EndpointCreate,
    access: ProjectOwnerDep,
    service: NotificationServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EndpointOut:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"notification.endpoint.create:{project_id}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=EndpointOut,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        ep = service.create_endpoint(
            project_id=project_id,
            channel_type=body.channel_type,
            name=body.name,
            config=body.config,
            secret_value=body.secret_value,
            created_by=access.user.persisted_id,
        )
        response = EndpointOut.model_validate(ep)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_201_CREATED)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@endpoints_router.patch("/{endpoint_id}", response_model=EndpointOut)
def update_endpoint(
    project_id: str,
    endpoint_id: str,
    body: EndpointUpdate,
    access: ProjectOwnerDep,
    service: NotificationServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EndpointOut:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"notification.endpoint.update:{project_id}:{endpoint_id}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=EndpointOut,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        ep = service.update_endpoint(
            endpoint_id,
            project_id=project_id,
            name=body.name,
            config=body.config,
            secret_value=body.secret_value,
            enabled=body.enabled,
        )
        response = EndpointOut.model_validate(ep)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_200_OK)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@endpoints_router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_endpoint(
    project_id: str,
    endpoint_id: str,
    access: ProjectOwnerDep,
    service: NotificationServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"notification.endpoint.delete:{project_id}:{endpoint_id}:{access.user.persisted_id}",
        payload={"endpoint_id": endpoint_id, "operation": "delete"},
        response_model=None,
    )
    if result.replayed:
        return
    try:
        service.delete_endpoint(endpoint_id, project_id)
        complete_idempotency(idempotency, result.reservation, {}, response_status=status.HTTP_204_NO_CONTENT)
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


# -- 事件订阅 ---------------------------------------------------------------


@subscriptions_router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(
    project_id: str,
    _access: ProjectAccessDep,
    service: NotificationServiceDep,
) -> list[SubscriptionOut]:
    subs = service.list_subscriptions(project_id)
    return [SubscriptionOut.model_validate(s) for s in subs]


@subscriptions_router.post("", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
def create_subscription(
    project_id: str,
    body: SubscriptionCreate,
    access: ProjectOperatorDep,
    service: NotificationServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SubscriptionOut:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"notification.subscription.create:{project_id}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=SubscriptionOut,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        sub = service.create_subscription(
            project_id=project_id,
            endpoint_id=body.endpoint_id,
            task_id=body.task_id,
            event_types=body.event_types,
            filter_json=body.filter_json,
            throttle_policy=body.throttle_policy,
            created_by=access.user.persisted_id,
        )
        response = SubscriptionOut.model_validate(sub)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_201_CREATED)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@subscriptions_router.patch("/{subscription_id}", response_model=SubscriptionOut)
def update_subscription(
    project_id: str,
    subscription_id: str,
    body: SubscriptionUpdate,
    access: ProjectOperatorDep,
    service: NotificationServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SubscriptionOut:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"notification.subscription.update:{project_id}:{subscription_id}:{access.user.persisted_id}",
        payload=body.model_dump(mode="json"),
        response_model=SubscriptionOut,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        sub = service.update_subscription(
            subscription_id,
            project_id=project_id,
            event_types=body.event_types,
            task_id=body.task_id,
            filter_json=body.filter_json,
            throttle_policy=body.throttle_policy,
            enabled=body.enabled,
        )
        response = SubscriptionOut.model_validate(sub)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_200_OK)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


@subscriptions_router.delete("/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subscription(
    project_id: str,
    subscription_id: str,
    access: ProjectOperatorDep,
    service: NotificationServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"notification.subscription.delete:{project_id}:{subscription_id}:{access.user.persisted_id}",
        payload={"subscription_id": subscription_id, "operation": "delete"},
        response_model=None,
    )
    if result.replayed:
        return
    try:
        service.delete_subscription(subscription_id, project_id)
        complete_idempotency(idempotency, result.reservation, {}, response_status=status.HTTP_204_NO_CONTENT)
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


# -- 投递状态 ---------------------------------------------------------------


@deliveries_router.get("", response_model=list[DeliveryOut])
def list_deliveries(
    project_id: str,
    _access: ProjectAccessDep,
    service: NotificationServiceDep,
    status_filter: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[DeliveryOut]:
    deliveries = service.list_deliveries(project_id, status=status_filter, limit=limit, offset=offset)
    return [DeliveryOut.model_validate(d) for d in deliveries]


@deliveries_router.get("/{delivery_id}", response_model=DeliveryOut)
def get_delivery(
    project_id: str,
    delivery_id: str,
    _access: ProjectAccessDep,
    service: NotificationServiceDep,
) -> DeliveryOut:
    d = service.get_delivery(delivery_id, project_id)
    if d is None:
        raise HTTPException(status_code=404, detail="投递记录不存在")
    return DeliveryOut.model_validate(d)


@deliveries_router.post("/{delivery_id}/retry", response_model=DeliveryOut)
def retry_delivery(
    project_id: str,
    delivery_id: str,
    access: ProjectOwnerDep,
    service: NotificationServiceDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DeliveryOut:
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"notification.delivery.retry:{project_id}:{delivery_id}:{access.user.persisted_id}",
        payload={"delivery_id": delivery_id, "operation": "retry"},
        response_model=DeliveryOut,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        d = service.retry_delivery(delivery_id, project_id)
        response = DeliveryOut.model_validate(d)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_200_OK)
        return response
    except ValueError as exc:
        release_idempotency(idempotency, result.reservation)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise


router.include_router(endpoints_router)
router.include_router(subscriptions_router)
router.include_router(deliveries_router)

__all__ = ["router"]
