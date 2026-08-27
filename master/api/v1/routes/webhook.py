"""CI/CD Webhook 公共入口（P8.3，§8.8）。

该端点不走项目成员权限，而是通过签名验证集成身份。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status

from master.api.v1.dependencies import CiIntegrationServiceDep

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/integrations",
    tags=["v1-ci-webhook"],
)


@router.post("/{integration_id}/webhook")
async def handle_webhook(
    integration_id: str,
    request: Request,
    service: CiIntegrationServiceDep,
) -> dict:
    """处理 CI/CD webhook（签名验证、delivery 去重、任务触发）。

    不走用户 JWT，通过签名验证集成身份。
    """
    delivery_id = request.headers.get("X-AETP-Delivery-Id", "")
    signature = request.headers.get("X-AETP-Signature", "")

    if not delivery_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少 X-AETP-Delivery-Id header",
        )

    body = await request.body()
    try:
        payload_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload_json = {}

    try:
        result = await service.handle_webhook(
            integration_id,
            delivery_id=delivery_id,
            signature=signature,
            payload_body=body,
            payload_json=payload_json,
            headers=dict(request.headers),
        )
    except ValueError as exc:
        detail = str(exc)
        if "签名" in detail:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "CI_WEBHOOK_SIGNATURE_INVALID",
                    "message": detail,
                    "data": None,
                },
            ) from exc
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail) from exc

    return {
        "status": result.status,
        "delivery_id": result.delivery_id,
        "triggered_run_ids": list(result.triggered_run_ids),
        "error": result.error,
    }
