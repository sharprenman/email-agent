"""版本化聊天和线程 API 路由。"""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Security
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader

from ..config import AuthContext, Settings
from ..observability import ObservationContext
from .errors import service_unavailable, translate_exception, unauthorized
from .schemas import (
    THREAD_ID_PATTERN,
    ApiResponse,
    ChatData,
    ChatRequest,
    DeleteThreadData,
    ErrorResponse,
    ResumeRequest,
    StreamEvent,
    StreamEventType,
    ThreadData,
)
from .service import AgentApplicationService

_service_token_header = APIKeyHeader(
    name="X-Service-Token",
    auto_error=False,
    description="生产环境必填的服务间认证令牌。",
)
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse}
    for status in (400, 401, 403, 404, 409, 422, 429, 503, 504)
}

api_router = APIRouter(prefix="/api/v1", tags=["邮件 Agent"])


def get_agent_service(request: Request) -> AgentApplicationService:
    """从应用生命周期读取已装配的 Agent 应用服务。"""
    service = getattr(request.app.state, "agent_service", None)
    if service is None:
        raise service_unavailable("Agent 运行时尚未装配")
    return service


async def require_auth(
    request: Request,
    token: Annotated[str | None, Security(_service_token_header)],
) -> AuthContext:
    """使用服务端配置解析可信身份，永远不相信请求正文中的用户字段。"""
    settings: Settings = request.app.state.settings
    expected = settings.service_auth_token
    if expected is not None:
        supplied = token or ""
        if not hmac.compare_digest(supplied, expected.get_secret_value()):
            raise unauthorized()
    auth: AuthContext = request.app.state.default_auth_context
    service = get_agent_service(request)
    if service.user_id != auth.user_id:
        raise service_unavailable("Agent 运行时身份配置不一致")
    return auth


@api_router.post(
    "/chat",
    response_model=ApiResponse[ChatData],
    responses=_ERROR_RESPONSES,
    summary="同步执行一次邮件 Agent 对话",
)
async def chat(
    payload: ChatRequest,
    request: Request,
    auth: Annotated[AuthContext, Security(require_auth)],
) -> ApiResponse[ChatData]:
    service = get_agent_service(request)
    data = await service.chat(
        payload,
        observation=_observation(request, auth),
    )
    return _success(request, data)


@api_router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    responses={
        **_ERROR_RESPONSES,
        200: {
            "description": "SSE 事件流",
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                }
            },
        },
    },
    summary="以 SSE 流式执行一次邮件 Agent 对话",
)
async def stream_chat(
    payload: ChatRequest,
    request: Request,
    auth: Annotated[AuthContext, Security(require_auth)],
) -> StreamingResponse:
    service = get_agent_service(request)
    observation = _observation(request, auth)

    async def generate() -> AsyncIterator[str]:
        current_thread_id = payload.thread_id or ""
        try:
            async for event in service.stream_chat(
                payload,
                observation=observation,
            ):
                current_thread_id = event.thread_id
                yield _sse(event, request)
        except Exception as exc:
            error = translate_exception(exc)
            yield _sse(
                StreamEvent(
                    type=StreamEventType.ERROR,
                    thread_id=current_thread_id,
                    data={
                        "code": error.code,
                        "message": error.message,
                        "request_id": request.state.request_id,
                        "trace_id": request.state.trace_id,
                    },
                ),
                request,
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@api_router.post(
    "/threads/{thread_id}/resume",
    response_model=ApiResponse[ChatData],
    responses=_ERROR_RESPONSES,
    summary="提交人工审批并恢复中断线程",
)
async def resume_thread(
    thread_id: Annotated[
        str,
        Path(min_length=8, max_length=128, pattern=THREAD_ID_PATTERN),
    ],
    payload: ResumeRequest,
    request: Request,
    auth: Annotated[AuthContext, Security(require_auth)],
) -> ApiResponse[ChatData]:
    service = get_agent_service(request)
    data = await service.resume(
        thread_id,
        payload,
        observation=_observation(request, auth),
    )
    return _success(request, data)


@api_router.get(
    "/threads/{thread_id}",
    response_model=ApiResponse[ThreadData],
    responses=_ERROR_RESPONSES,
    summary="读取线程公开状态",
)
async def get_thread(
    thread_id: Annotated[
        str,
        Path(min_length=8, max_length=128, pattern=THREAD_ID_PATTERN),
    ],
    request: Request,
    auth: Annotated[AuthContext, Security(require_auth)],
) -> ApiResponse[ThreadData]:
    service = get_agent_service(request)
    data = await service.get_thread(
        thread_id,
        observation=_observation(request, auth),
    )
    return _success(request, data)


@api_router.delete(
    "/threads/{thread_id}",
    response_model=ApiResponse[DeleteThreadData],
    responses=_ERROR_RESPONSES,
    summary="删除线程检查点",
)
async def delete_thread(
    thread_id: Annotated[
        str,
        Path(min_length=8, max_length=128, pattern=THREAD_ID_PATTERN),
    ],
    request: Request,
    auth: Annotated[AuthContext, Security(require_auth)],
) -> ApiResponse[DeleteThreadData]:
    service = get_agent_service(request)
    data = await service.delete_thread(
        thread_id,
        observation=_observation(request, auth),
    )
    return _success(request, data)


def _success(request: Request, data: Any) -> ApiResponse[Any]:
    return ApiResponse(
        data=data,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )


def _observation(
    request: Request,
    auth: AuthContext,
) -> ObservationContext:
    return request.app.state.observability.context(
        user_id=auth.user_id,
        request_id=request.state.request_id,
        trace_id=request.state.trace_id,
    )


def _sse(event: StreamEvent, request: Request) -> str:
    payload = event.model_dump(mode="json")
    payload["request_id"] = request.state.request_id
    payload["trace_id"] = request.state.trace_id
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        f"event: {event.type.value}\n"
        f"id: {request.state.trace_id}\n"
        f"data: {data}\n\n"
    )
