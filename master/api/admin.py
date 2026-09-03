"""平台管理员路由：用户审核和平台角色管理。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, status

from master.api.dependencies import AuthDep, IdempotencyServiceDep
from master.api.idempotency import complete as complete_idempotency
from master.api.idempotency import release as release_idempotency
from master.api.idempotency import reserve_or_replay
from master.api.permissions import PlatformAdminDep
from master.api.schemas import AdminUserOut, UserApprovalRequest

router = APIRouter(prefix="/api/v2/users", tags=["admin"])


@router.get("", response_model=list[AdminUserOut])
def list_users(
    auth: AuthDep,
    _admin: PlatformAdminDep,
    account_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AdminUserOut]:
    """分页查询用户列表；管理员按 account_status 筛选 pending 待审批账户。"""
    users = auth.list_users(account_status=account_status, limit=limit, offset=offset)
    return [AdminUserOut.model_validate(user) for user in users]


@router.patch("/{user_id}", response_model=AdminUserOut)
def approve_user(
    user_id: int,
    body: UserApprovalRequest,
    auth: AuthDep,
    admin: PlatformAdminDep,
    idempotency: IdempotencyServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AdminUserOut:
    """审批用户或修改平台角色。"""
    result = reserve_or_replay(
        idempotency,
        idempotency_key,
        scope=f"admin.user.update:{admin.persisted_id}:{user_id}",
        payload=body.model_dump(mode="json"),
        response_model=AdminUserOut,
    )
    if result.replayed:
        assert result.response is not None
        return result.response
    try:
        user = auth.approve_user(
            user_id,
            account_status=(body.account_status.value if body.account_status is not None else None),
            platform_role=(body.platform_role.value if body.platform_role is not None else None),
            actor_id=admin.persisted_id,
        )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在",
            )
        response = AdminUserOut.model_validate(user)
        complete_idempotency(idempotency, result.reservation, response, response_status=status.HTTP_200_OK)
        return response
    except HTTPException:
        release_idempotency(idempotency, result.reservation)
        raise
    except Exception:
        release_idempotency(idempotency, result.reservation)
        raise
