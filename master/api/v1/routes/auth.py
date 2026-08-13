"""v1 认证路由：注册、登录、刷新、登出、当前用户。"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status

from master.config import get_settings
from master.domain.enums import AccountStatus
from master.domain.models import User
from master.domain.time import utcnow

from master.api.v1.dependencies import AuthDep, CurrentUser
from master.api.v1.rate_limit import (
    client_ip,
    login_limiter,
    refresh_limiter,
    register_limiter,
)
from master.api.v1.schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from master.api.v1.security import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from master.application.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_token_response(auth: AuthService, user: User) -> TokenResponse:
    """签发访问令牌 + 刷新令牌（刷新令牌只存哈希入库，P2.10）。"""
    if user.id is None:
        # 持久化用户必有主键；防御性校验避免把 None 写入会话表
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="用户主键缺失",
        )
    settings = get_settings()
    raw_refresh = generate_refresh_token()
    expires_at = utcnow() + timedelta(days=settings.refresh_token_expire_days)
    auth.issue_refresh_token(user.id, hash_refresh_token(raw_refresh), expires_at)
    return TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=raw_refresh,
        expires_in=settings.jwt_expire_minutes * 60,
    )


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
    return _issue_token_response(auth, user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest, auth: AuthDep, request: Request) -> TokenResponse:
    """用刷新令牌换新令牌对（旋转：旧令牌同一事务内撤销，防重放）。"""
    ip = client_ip(request)
    if not refresh_limiter.allow(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="刷新过于频繁，请稍后再试",
        )
    settings = get_settings()
    raw_new = generate_refresh_token()
    expires_at = utcnow() + timedelta(days=settings.refresh_token_expire_days)
    user = auth.rotate_refresh_token(
        hash_refresh_token(body.refresh_token),
        hash_refresh_token(raw_new),
        expires_at,
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="刷新令牌无效、已过期或已撤销",
        )
    refresh_limiter.reset(ip)
    return TokenResponse(
        access_token=create_access_token(str(user.id)),
        refresh_token=raw_new,
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(auth: AuthDep, body: LogoutRequest | None = None) -> None:
    """登出：撤销携带的刷新令牌（访问令牌短有效期自然失效，P2.10）。"""
    if body is not None and body.refresh_token:
        auth.revoke_refresh_token(hash_refresh_token(body.refresh_token))


@router.get("/me", response_model=UserOut)
def me(current_user: CurrentUser) -> UserOut:
    """获取当前登录用户。"""
    return UserOut.model_validate(current_user)
