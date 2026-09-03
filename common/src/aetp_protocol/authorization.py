""" RBAC 与项目范围授权契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from .errors import ErrorCode
from .ids import BusinessId


class PlatformRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class ProjectRole(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    MAINTAINER = "maintainer"
    OWNER = "owner"


class Action(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"
    CANCEL = "cancel"
    ADMIN = "admin"


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=256)
    display_name: str = ""
    platform_roles: tuple[PlatformRole, ...] = ()


class AuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: Principal
    project_id: BusinessId | None = None
    object_type: str = Field(min_length=1, max_length=128)
    object_id: BusinessId | None = None
    action: Action


class AuthorizationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: ErrorCode


class ProjectScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: BusinessId
    role: ProjectRole


class AuthorizationService(Protocol):
    def check(self, request: AuthorizationRequest) -> AuthorizationDecision: ...


_PROJECT_ACTIONS: dict[ProjectRole, frozenset[Action]] = {
    ProjectRole.VIEWER: frozenset({Action.READ}),
    ProjectRole.OPERATOR: frozenset({Action.READ, Action.EXECUTE, Action.CANCEL}),
    ProjectRole.MAINTAINER: frozenset(
        {Action.READ, Action.CREATE, Action.UPDATE, Action.EXECUTE, Action.CANCEL}
    ),
    ProjectRole.OWNER: frozenset(
        {Action.READ, Action.CREATE, Action.UPDATE, Action.DELETE, Action.EXECUTE, Action.CANCEL, Action.ADMIN}
    ),
}


def decide_project_access(request: AuthorizationRequest, role: ProjectRole | None) -> AuthorizationDecision:
    """按固定角色矩阵做 fail-closed 授权判定。"""
    if PlatformRole.ADMIN in request.principal.platform_roles:
        return AuthorizationDecision(allowed=True, reason_code=ErrorCode("AUTHORIZATION_ALLOWED"))
    if request.project_id is None or role is None:
        return AuthorizationDecision(allowed=False, reason_code=ErrorCode("PROJECT_SCOPE_REQUIRED"))
    if request.action in _PROJECT_ACTIONS[role]:
        return AuthorizationDecision(allowed=True, reason_code=ErrorCode("AUTHORIZATION_ALLOWED"))
    return AuthorizationDecision(allowed=False, reason_code=ErrorCode("AUTHORIZATION_DENIED"))
