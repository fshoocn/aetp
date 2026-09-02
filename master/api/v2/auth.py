"""V2 认证 API。"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from master.api.v1.dependencies import AuthDep, CurrentUser
from master.api.v1.rate_limit import client_ip, login_limiter, refresh_limiter, register_limiter
from master.api.v1.security import create_access_token, generate_refresh_token, hash_refresh_token
from master.application.services.auth_service import AuthService
from master.config import get_settings
from master.domain.enums import AccountStatus
from master.domain.models import User
from master.domain.time import utcnow

router = APIRouter(prefix="/api/v2/auth", tags=["v2-auth"])


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str
    password: str


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    refresh_token: str


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    refresh_token: str | None = None


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    id: int
    username: str
    display_name: str
    account_status: str
    platform_role: str


class TokenView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


def _token_response(auth: AuthService, user: User) -> TokenView:
    if user.id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="用户主键缺失")
    raw_refresh = generate_refresh_token()
    expires_at = utcnow() + timedelta(days=get_settings().refresh_token_expire_days)
    auth.issue_refresh_token(user.id, hash_refresh_token(raw_refresh), expires_at)
    return TokenView(
        access_token=create_access_token(str(user.id)),
        refresh_token=raw_refresh,
        expires_in=get_settings().jwt_expire_minutes * 60,
    )


@router.post("/register", response_model=UserView, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, auth: AuthDep, request: Request) -> UserView:
    if not register_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="注册过于频繁，请稍后再试")
    return UserView.model_validate(
        auth.create_user(
            username=body.username,
            password=body.password,
            display_name=body.display_name,
        )
    )


@router.post("/login", response_model=TokenView)
def login(body: LoginRequest, auth: AuthDep, request: Request) -> TokenView:
    ip = client_ip(request)
    if not login_limiter.allow(ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="登录尝试过于频繁，请稍后再试")
    user = auth.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if user.account_status is AccountStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户尚未通过审批，请联系平台管理员")
    if user.account_status is AccountStatus.DISABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用，请联系平台管理员")
    login_limiter.reset(ip)
    return _token_response(auth, user)


@router.post("/refresh", response_model=TokenView)
def refresh(body: RefreshRequest, auth: AuthDep, request: Request) -> TokenView:
    ip = client_ip(request)
    if not refresh_limiter.allow(ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="刷新过于频繁，请稍后再试")
    raw_new = generate_refresh_token()
    user = auth.rotate_refresh_token(
        hash_refresh_token(body.refresh_token),
        hash_refresh_token(raw_new),
        utcnow() + timedelta(days=get_settings().refresh_token_expire_days),
    )
    if user is None or user.id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="刷新令牌无效、已过期或已撤销")
    refresh_limiter.reset(ip)
    return TokenView(
        access_token=create_access_token(str(user.id)),
        refresh_token=raw_new,
        expires_in=get_settings().jwt_expire_minutes * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(auth: AuthDep, body: LogoutRequest | None = None) -> None:
    if body is not None and body.refresh_token:
        auth.revoke_refresh_token(hash_refresh_token(body.refresh_token))


@router.get("/me", response_model=UserView)
def me(current_user: CurrentUser) -> UserView:
    return UserView.model_validate(current_user)


__all__ = ["router"]
