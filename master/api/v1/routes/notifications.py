"""项目范围通知端点 / 事件订阅 / 投递状态 API（P7.6，§10.5）。

密钥永不回显：API 响应不包含 secret_value；配置中如含 webhook token
等敏感字段由调用方自行脱敏。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from master.api.v1.dependencies import NotificationServiceDep
from master.api.v1.permissions import (
    ProjectAccessDep,
    ProjectManagerDep,
    ProjectOwnerDep,
)
from master.api.v1.schemas import (
    DeliveryOut,
    EndpointCreate,
    EndpointOut,
    EndpointUpdate,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionUpdate,
)

logger = logging.getLogger(__name__)

endpoints_router = APIRouter(
    prefix="/projects/{project_id}/notification-endpoints",
    tags=["v1-notification-endpoints"],
)

subscriptions_router = APIRouter(
    prefix="/projects/{project_id}/event-subscriptions",
    tags=["v1-event-subscriptions"],
)

deliveries_router = APIRouter(
    prefix="/projects/{project_id}/event-deliveries",
    tags=["v1-event-deliveries"],
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
) -> EndpointOut:
    try:
        ep = service.create_endpoint(
            project_id=project_id,
            channel_type=body.channel_type,
            name=body.name,
            config=body.config,
            secret_value=body.secret_value,
            created_by=access.user.persisted_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return EndpointOut.model_validate(ep)


@endpoints_router.patch("/{endpoint_id}", response_model=EndpointOut)
def update_endpoint(
    project_id: str,
    endpoint_id: str,
    body: EndpointUpdate,
    _access: ProjectOwnerDep,
    service: NotificationServiceDep,
) -> EndpointOut:
    try:
        ep = service.update_endpoint(
            endpoint_id,
            project_id=project_id,
            name=body.name,
            config=body.config,
            secret_value=body.secret_value,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return EndpointOut.model_validate(ep)


@endpoints_router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_endpoint(
    project_id: str,
    endpoint_id: str,
    _access: ProjectOwnerDep,
    service: NotificationServiceDep,
) -> None:
    try:
        service.delete_endpoint(endpoint_id, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
    access: ProjectManagerDep,
    service: NotificationServiceDep,
) -> SubscriptionOut:
    try:
        sub = service.create_subscription(
            project_id=project_id,
            endpoint_id=body.endpoint_id,
            event_types=body.event_types,
            filter_json=body.filter_json,
            throttle_policy=body.throttle_policy,
            created_by=access.user.persisted_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return SubscriptionOut.model_validate(sub)


@subscriptions_router.patch("/{subscription_id}", response_model=SubscriptionOut)
def update_subscription(
    project_id: str,
    subscription_id: str,
    body: SubscriptionUpdate,
    _access: ProjectManagerDep,
    service: NotificationServiceDep,
) -> SubscriptionOut:
    try:
        sub = service.update_subscription(
            subscription_id,
            project_id=project_id,
            event_types=body.event_types,
            filter_json=body.filter_json,
            throttle_policy=body.throttle_policy,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return SubscriptionOut.model_validate(sub)


@subscriptions_router.delete("/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subscription(
    project_id: str,
    subscription_id: str,
    _access: ProjectManagerDep,
    service: NotificationServiceDep,
) -> None:
    try:
        service.delete_subscription(subscription_id, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
    _access: ProjectOwnerDep,
    service: NotificationServiceDep,
) -> DeliveryOut:
    try:
        d = service.retry_delivery(delivery_id, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return DeliveryOut.model_validate(d)
