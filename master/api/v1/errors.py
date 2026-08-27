"""v1 API 的应用异常 HTTP 映射。"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
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

_HTTP_ERROR_CODES = {
    400: "INVALID_REQUEST",
    401: "AUTH_REQUIRED",
    403: "AUTH_FORBIDDEN",
    404: "RESOURCE_NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
}


def _status_code_for(error: ApplicationError) -> int:
    """根据异常类型返回 HTTP 状态码。"""
    for error_type, status_code in _STATUS_BY_ERROR.items():
        if isinstance(error, error_type):
            return status_code
    return 400


def _error_body(code: str, message: str, data: object = None) -> dict[str, object]:
    """全局约定的三段式错误响应体：{"code","message","data"}。"""
    return {"code": code, "message": message, "data": data}


async def application_error_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """将应用层异常转换为三段式错误响应 {"code","message","data"}。"""
    _ = request
    if not isinstance(error, ApplicationError):
        logger.exception("未处理的应用异常", exc_info=error)
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "服务器内部错误"),
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
        content=_error_body(error.code, str(error)),
    )


async def http_exception_handler(request: Request, error: Exception) -> JSONResponse:
    """将路由/权限 HTTPException 也收敛为三段式错误响应。"""
    _ = request
    http_error = cast(HTTPException, error)
    detail = http_error.detail
    if isinstance(detail, dict) and {"code", "message", "data"}.issubset(detail):
        content = detail
    else:
        content = _error_body(
            _HTTP_ERROR_CODES.get(http_error.status_code, "HTTP_ERROR"),
            str(detail),
        )
    return JSONResponse(
        status_code=http_error.status_code,
        content=jsonable_encoder(content),
        headers=http_error.headers,
    )


async def request_validation_error_handler(request: Request, error: Exception) -> JSONResponse:
    """将 FastAPI 请求校验错误转换为统一 VALIDATION_ERROR。"""
    _ = request
    validation_error = cast(RequestValidationError, error)
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", "请求参数校验失败", jsonable_encoder(validation_error.errors())),
    )


def register_application_error_handlers(app: FastAPI) -> None:
    """向 FastAPI 注册统一的应用异常处理器。"""
    app.add_exception_handler(ApplicationError, application_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
