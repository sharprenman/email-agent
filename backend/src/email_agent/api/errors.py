"""API 业务错误、异常映射和安全响应。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..contracts import (
    ProviderAuthenticationError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from .schemas import ErrorResponse


@dataclass(frozen=True)
class ApiError(Exception):
    """可安全返回给客户端的业务异常。"""

    status_code: int
    code: int
    message: str


def bad_request(message: str) -> ApiError:
    return ApiError(400, 40001, message)


def unauthorized() -> ApiError:
    return ApiError(401, 40100, "认证信息无效")


def forbidden(message: str = "无权访问该资源") -> ApiError:
    return ApiError(403, 40300, message)


def not_found(message: str = "资源不存在") -> ApiError:
    return ApiError(404, 40400, message)


def conflict(message: str) -> ApiError:
    return ApiError(409, 40900, message)


def rate_limited() -> ApiError:
    return ApiError(429, 42900, "上游服务请求受限，请稍后重试")


def service_unavailable(message: str = "服务暂时不可用") -> ApiError:
    return ApiError(503, 50300, message)


def gateway_timeout() -> ApiError:
    return ApiError(504, 50400, "Agent 执行超时")


def translate_exception(exc: Exception) -> ApiError:
    """把已知内部异常转换为不含敏感细节的 HTTP 错误。"""
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, ProviderRateLimitError):
        return rate_limited()
    if isinstance(exc, ProviderTimeoutError | asyncio.TimeoutError):
        return gateway_timeout()
    if isinstance(exc, ProviderNotFoundError):
        return not_found("上游资源不存在")
    if isinstance(exc, ProviderPermissionError):
        return forbidden("外部服务权限不足")
    if isinstance(
        exc,
        ProviderAuthenticationError | ProviderUnavailableError | UnsupportedCapabilityError,
    ):
        return service_unavailable()
    return ApiError(500, 50000, "系统内部错误")


def register_exception_handlers(application: FastAPI) -> None:
    """注册统一错误响应，禁止默认返回内部异常细节。"""

    @application.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(request, exc)

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            ApiError(422, 42200, "请求参数校验失败"),
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return _error_response(request, translate_exception(exc))


def _error_response(request: Request, error: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    trace_id = getattr(request.state, "trace_id", "unknown")
    payload = ErrorResponse(
        code=error.code,
        message=error.message,
        request_id=request_id,
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=error.status_code,
        content=payload.model_dump(mode="json"),
        headers={
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
        },
    )
