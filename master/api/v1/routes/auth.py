"""v1 认证路由：注册、登录、当前用户。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from master.domain.enums import AccountStatus

from master.api.v1.dependencies import AuthDep, CurrentUser
from master.api.v1.rate_limit import client_ip, login_limiter, register_limiter
from master.api.v1.schemas import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from master.api.v1.security import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterRequest, auth: AuthDep, request: Request
) -> UserOut:
    """注册新用户，初始状态为 pending；按 IP 限流防滥用。"""
    if not register_limiter.allow(client_ip(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="注册过于频繁，请稍后再试",
        )
    user = auth.create_user(
        username=body.username,
        password=body.password,
        display_name=body.display_name,
    )
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, auth: AuthDep, request: Request) -> TokenResponse:
    """登录并返回 JWT 访问令牌；按 IP 限流防爆破。"""
    ip = client_ip(request)
    if not login_limiter.allow(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试过于频繁，请稍后再试",
        )
    user = auth.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )
    if user.account_status == AccountStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账户尚未通过审批，请联系平台管理员",
        )
    if user.account_status == AccountStatus.DISABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账户已被禁用，请联系平台管理员",
        )
    login_limiter.reset(ip)
    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserOut)
def me(current_user: CurrentUser) -> UserOut:
    """获取当前登录用户。"""
    return UserOut.model_validate(current_user)
