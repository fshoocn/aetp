"""v1 平台管理员路由：用户审核和平台角色管理。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from master.api.v1.dependencies import AuthDep
from master.api.v1.permissions import PlatformAdminDep
from master.api.v1.schemas import AdminUserOut, UserApprovalRequest

router = APIRouter(prefix="/users", tags=["admin"])


@router.get("", response_model=list[AdminUserOut])
def list_users(
    auth: AuthDep,
    _admin: PlatformAdminDep,
    account_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AdminUserOut]:
    """分页查询用户列表；管理员按 account_status 筛选 pending 待审批账户。"""
    users = auth.list_users(
        account_status=account_status, limit=limit, offset=offset
    )
    return [AdminUserOut.model_validate(user) for user in users]


@router.patch("/{user_id}", response_model=AdminUserOut)
def approve_user(
    user_id: int,
    body: UserApprovalRequest,
    auth: AuthDep,
    _admin: PlatformAdminDep,
) -> AdminUserOut:
    """审批用户或修改平台角色。"""
    user = auth.approve_user(
        user_id,
        account_status=(
            body.account_status.value if body.account_status is not None else None
        ),
        platform_role=(
            body.platform_role.value if body.platform_role is not None else None
        ),
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )
    return AdminUserOut.model_validate(user)
