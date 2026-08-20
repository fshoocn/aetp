"""v1 API 的应用异常 HTTP 映射。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from master.application.errors import (
    ApplicationError,
    DeviceNotFoundError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidProjectOwnerError,
    InvalidRoleGrantError,
    LastOwnerError,
    MemberAlreadyExistsError,
    MemberNotFoundError,
    NodeAlreadyBoundError,
    NodeBindingNotFoundError,
    NodeCapabilityMismatchError,
    NodeDisabledError,
    NodeNotFoundError,
    ProjectAccessDeniedError,
    ProjectKeyAlreadyExistsError,
    ProjectNotFoundError,
    RunNotFoundError,
    ScriptNotFoundError,
    TaskNotFoundError,
    UsernameAlreadyExistsError,
)

_STATUS_BY_ERROR: dict[type[ApplicationError], int] = {
    ProjectNotFoundError: 404,
    MemberNotFoundError: 404,
    MemberAlreadyExistsError: 409,
    LastOwnerError: 409,
    InactiveUserError: 409,
    InvalidRoleGrantError: 403,
    NodeNotFoundError: 404,
    DeviceNotFoundError: 404,
    NodeDisabledError: 409,
    NodeCapabilityMismatchError: 409,
    NodeBindingNotFoundError: 404,
    NodeAlreadyBoundError: 409,
    UsernameAlreadyExistsError: 409,
    InvalidCredentialsError: 401,
    ProjectKeyAlreadyExistsError: 409,
    ProjectAccessDeniedError: 403,
    InvalidProjectOwnerError: 422,
    ScriptNotFoundError: 404,
    TaskNotFoundError: 404,
    RunNotFoundError: 404,
}
logger = logging.getLogger(__name__)


def _status_code_for(error: ApplicationError) -> int:
    """根据异常类型返回 HTTP 状态码。"""
    for error_type, status_code in _STATUS_BY_ERROR.items():
        if isinstance(error, error_type):
            return status_code
    return 400


async def application_error_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """将应用层异常转换为兼容现有 API 的 detail 响应。"""
    _ = request
    if not isinstance(error, ApplicationError):
        logger.exception("未处理的应用异常", exc_info=error)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务器内部错误"},
        )
    status_code = _status_code_for(error)
    logger.warning(
        "应用业务异常: type=%s, status=%s, detail=%s",
        type(error).__name__,
        status_code,
        error,
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(error)},
    )


def register_application_error_handlers(app: FastAPI) -> None:
    """向 FastAPI 注册统一的应用异常处理器。"""
    app.add_exception_handler(ApplicationError, application_error_handler)
