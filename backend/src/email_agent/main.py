"""FastAPI 应用入口与基础中间件。"""

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .api import AgentApplicationService, api_router
from .api.errors import register_exception_handlers, service_unavailable
from .api.schemas import ApiResponse, ErrorResponse, ReadyData
from .bootstrap import open_agent_service
from .config import Settings, build_default_auth_context, get_settings
from .observability import Observability

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class HealthData(BaseModel):
    """存活检查数据。"""

    status: Literal["ok"]


class HealthResponse(BaseModel):
    """统一的存活检查响应。"""

    code: Literal[0]
    message: Literal["success"]
    data: HealthData


class RequestContextMiddleware(BaseHTTPMiddleware):
    """生成 request/trace ID，并执行 Content-Length 上限检查。"""

    def __init__(
        self,
        app,
        *,
        max_request_bytes: int,
        max_attachment_bytes: int,
        observability: Observability,
        user_id: str,
    ) -> None:
        super().__init__(app)
        self._max_request_bytes = max_request_bytes
        self._max_attachment_bytes = max_attachment_bytes
        self._observability = observability
        self._user_id = user_id

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = _safe_id(request.headers.get("X-Request-ID"))
        request.state.trace_id = _safe_id(request.headers.get("X-Trace-ID"))
        context = self._observability.context(
            user_id=self._user_id,
            request_id=request.state.request_id,
            trace_id=request.state.trace_id,
        )
        started = time.perf_counter()
        content_length = request.headers.get("Content-Length")
        request_limit = (
            self._max_attachment_bytes
            if request.method == "POST" and request.url.path == "/api/v1/files"
            else self._max_request_bytes
        )
        if content_length is not None:
            try:
                parsed_length = int(content_length)
                too_large = parsed_length < 0 or parsed_length > request_limit
            except ValueError:
                too_large = True
            if too_large:
                self._observability.record_operation(
                    context,
                    category="http",
                    operation=f"{request.method.lower()}.request",
                    outcome="error",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error_type="RequestTooLarge",
                )
                payload = ErrorResponse(
                    code=40001,
                    message="请求体超过服务端限制",
                    request_id=request.state.request_id,
                    trace_id=request.state.trace_id,
                )
                return JSONResponse(
                    status_code=400,
                    content=payload.model_dump(mode="json"),
                    headers={
                        "X-Request-ID": request.state.request_id,
                        "X-Trace-ID": request.state.trace_id,
                    },
                )
        try:
            response = await call_next(request)
        except BaseException as exc:
            self._observability.record_operation(
                context,
                category="http",
                operation=f"{request.method.lower()}.request",
                outcome="error",
                duration_ms=(time.perf_counter() - started) * 1000,
                error_type=type(exc).__name__,
            )
            raise
        route = request.scope.get("route")
        route_path = getattr(route, "path", "request")
        self._observability.record_operation(
            context,
            category="http",
            operation=f"{request.method.lower()}.{route_path}",
            outcome="success" if response.status_code < 400 else "error",
            duration_ms=(time.perf_counter() - started) * 1000,
            error_type=None if response.status_code < 400 else f"HTTP{response.status_code}",
        )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Trace-ID"] = request.state.trace_id
        return response


def create_app(
    settings: Settings | None = None,
    *,
    agent_service: AgentApplicationService | None = None,
    observability: Observability | None = None,
    auto_configure: bool = False,
) -> FastAPI:
    """创建 FastAPI 应用。"""
    effective_settings = settings or get_settings()
    service_observability = getattr(agent_service, "observability", None)
    if (
        observability is not None
        and service_observability is not None
        and observability is not service_observability
    ):
        raise ValueError("API 与 Agent 必须共用同一个可观测性实例")
    effective_observability = (
        observability
        or service_observability
        or Observability()
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if (
            agent_service is not None
            or not auto_configure
            or effective_settings.mail_provider is None
        ):
            yield
            return
        async with open_agent_service(
            effective_settings,
            effective_observability,
        ) as configured_service:
            application.state.agent_service = configured_service
            try:
                yield
            finally:
                application.state.agent_service = None

    application = FastAPI(
        title="DeepAgents 邮件智能体 API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = effective_settings
    application.state.default_auth_context = build_default_auth_context(effective_settings)
    application.state.agent_service = agent_service
    application.state.observability = effective_observability
    application.add_middleware(
        RequestContextMiddleware,
        max_request_bytes=effective_settings.max_request_bytes,
        max_attachment_bytes=effective_settings.max_attachment_bytes,
        observability=effective_observability,
        user_id=effective_settings.single_user_id,
    )
    register_exception_handlers(application)
    application.include_router(api_router)

    @application.get("/health/live", response_model=HealthResponse, tags=["健康检查"])
    def health_live() -> HealthResponse:
        """返回进程存活状态，不访问任何外部依赖。"""
        return HealthResponse(
            code=0,
            message="success",
            data=HealthData(status="ok"),
        )

    @application.get(
        "/health/ready",
        response_model=ApiResponse[ReadyData],
        responses={503: {"model": ErrorResponse}},
        tags=["健康检查"],
    )
    async def health_ready(request: Request) -> ApiResponse[ReadyData]:
        """仅在 Agent 与持久化资源完成装配后返回就绪。"""
        service = application.state.agent_service
        if service is None:
            raise service_unavailable("Agent 运行时尚未装配")
        check_ready = getattr(service, "check_ready", None)
        ready = await check_ready() if check_ready is not None else service.is_ready()
        if not ready:
            raise service_unavailable("Agent 运行时尚未装配")
        return ApiResponse(
            data=ReadyData(
                status="ready",
                agent_runtime="configured",
                persistence="configured",
            ),
            request_id=request.state.request_id,
            trace_id=request.state.trace_id,
        )

    return application


def _safe_id(value: str | None) -> str:
    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return uuid.uuid4().hex


app = create_app(auto_configure=True)
