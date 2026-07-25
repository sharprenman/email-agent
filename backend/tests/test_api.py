"""FastAPI 聊天、线程、认证和错误契约测试。"""

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from email_agent.api.errors import conflict, forbidden, not_found
from email_agent.api.schemas import (
    ChatData,
    DeleteThreadData,
    StreamEvent,
    StreamEventType,
    ThreadData,
    ThreadStatus,
)
from email_agent.config import AppEnvironment, Settings
from email_agent.contracts import (
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from email_agent.main import create_app

THREAD_ID = "th_0123456789abcdef0123456789abcdef"
VALID_CHAT = {
    "message": "总结最近邮件",
    "idempotency_key": "chat-request-0001",
}


@dataclass
class _FakeService:
    error: Exception | None = None
    user_id: str = "private-owner"

    def is_ready(self) -> bool:
        return True

    async def chat(self, payload, **kwargs) -> ChatData:
        del payload, kwargs
        self._raise()
        return ChatData(
            thread_id=THREAD_ID,
            status=ThreadStatus.COMPLETED,
            message_count=2,
        )

    async def stream_chat(self, payload, **kwargs):
        del payload, kwargs
        self._raise()
        yield StreamEvent(
            type=StreamEventType.THREAD,
            thread_id=THREAD_ID,
            data={"status": "started"},
        )
        yield StreamEvent(
            type=StreamEventType.COMPLETED,
            thread_id=THREAD_ID,
            data={"status": "completed"},
        )

    async def resume(self, thread_id, payload, **kwargs) -> ChatData:
        del payload, kwargs
        self._raise()
        return ChatData(
            thread_id=thread_id,
            status=ThreadStatus.COMPLETED,
            message_count=4,
        )

    async def get_thread(self, thread_id, **kwargs) -> ThreadData:
        del kwargs
        self._raise()
        return ThreadData(
            thread_id=thread_id,
            status=ThreadStatus.COMPLETED,
            message_count=2,
        )

    async def delete_thread(self, thread_id, **kwargs) -> DeleteThreadData:
        del kwargs
        self._raise()
        return DeleteThreadData(thread_id=thread_id)

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error


def _settings(**updates: Any) -> Settings:
    return Settings(
        app_env=AppEnvironment.TEST,
        single_user_id="private-owner",
        service_auth_token="service-token",
        max_request_bytes=1024,
        **updates,
    )


async def _request(
    service: _FakeService | None,
    method: str,
    path: str,
    *,
    json: Any = None,
    headers: dict[str, str] | None = None,
    settings: Settings | None = None,
) -> httpx.Response:
    app = create_app(settings or _settings(), agent_service=service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(
            method,
            path,
            json=json,
            headers=headers,
        )


def _auth() -> dict[str, str]:
    return {
        "X-Service-Token": "service-token",
        "X-Request-ID": "request-123",
        "X-Trace-ID": "trace-123",
    }


def test_chat_success_uses_unified_response_and_trace_headers() -> None:
    response = asyncio.run(
        _request(_FakeService(), "POST", "/api/v1/chat", json=VALID_CHAT, headers=_auth())
    )

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "success",
        "data": {
            "thread_id": THREAD_ID,
            "status": "completed",
            "message_count": 2,
            "pending_approvals": [],
            "result": None,
            "updated_at": None,
        },
        "request_id": "request-123",
        "trace_id": "trace-123",
    }
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.headers["X-Trace-ID"] == "trace-123"


def test_service_auth_rejects_missing_or_invalid_token() -> None:
    missing = asyncio.run(
        _request(_FakeService(), "POST", "/api/v1/chat", json=VALID_CHAT)
    )
    invalid = asyncio.run(
        _request(
            _FakeService(),
            "POST",
            "/api/v1/chat",
            json=VALID_CHAT,
            headers={"X-Service-Token": "wrong-token"},
        )
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.json()["code"] == 40100
    assert "service-token" not in missing.text


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "", "idempotency_key": "chat-request-0001"},
        {"message": "测试", "idempotency_key": "short"},
        {**VALID_CHAT, "user_id": "attacker"},
        {
            **VALID_CHAT,
            "attachments": [
                {"file_id": "file-1"},
                {"file_id": "file-1"},
            ],
        },
    ],
)
def test_chat_validation_returns_safe_422(payload: dict[str, Any]) -> None:
    response = asyncio.run(
        _request(_FakeService(), "POST", "/api/v1/chat", json=payload, headers=_auth())
    )

    assert response.status_code == 422
    assert response.json()["code"] == 42200
    assert "errors" not in response.json()


def test_request_size_limit_returns_400() -> None:
    response = asyncio.run(
        _request(
            _FakeService(),
            "POST",
            "/api/v1/chat",
            json={"message": "x" * 1100, "idempotency_key": "chat-request-0001"},
            headers=_auth(),
        )
    )

    assert response.status_code == 400
    assert response.json()["code"] == 40001


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (forbidden(), 403, 40300),
        (not_found(), 404, 40400),
        (conflict("幂等键重复"), 409, 40900),
        (ProviderRateLimitError("raw upstream 429"), 429, 42900),
        (
            ProviderUnavailableError(
                "token=secret /Users/private/project internal upstream response"
            ),
            503,
            50300,
        ),
        (ProviderTimeoutError("raw timeout"), 504, 50400),
    ],
)
def test_known_errors_map_to_safe_contract(
    error: Exception,
    status: int,
    code: int,
) -> None:
    response = asyncio.run(
        _request(
            _FakeService(error=error),
            "POST",
            "/api/v1/chat",
            json=VALID_CHAT,
            headers=_auth(),
        )
    )

    assert response.status_code == status
    assert response.json()["code"] == code
    assert "secret" not in response.text
    assert "/Users/" not in response.text
    assert "upstream response" not in response.text


def test_unconfigured_runtime_returns_503_and_liveness_stays_available() -> None:
    chat = asyncio.run(
        _request(None, "POST", "/api/v1/chat", json=VALID_CHAT, headers=_auth())
    )
    ready = asyncio.run(_request(None, "GET", "/health/ready"))
    live = asyncio.run(_request(None, "GET", "/health/live"))

    assert chat.status_code == 503
    assert ready.status_code == 503
    assert live.status_code == 200


def test_ready_and_thread_routes_use_unified_contract() -> None:
    service = _FakeService()
    ready = asyncio.run(_request(service, "GET", "/health/ready"))
    thread = asyncio.run(
        _request(
            service,
            "GET",
            f"/api/v1/threads/{THREAD_ID}",
            headers=_auth(),
        )
    )
    deleted = asyncio.run(
        _request(
            service,
            "DELETE",
            f"/api/v1/threads/{THREAD_ID}",
            headers=_auth(),
        )
    )

    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "ready"
    assert thread.status_code == 200
    assert deleted.json()["data"] == {"thread_id": THREAD_ID, "deleted": True}


def test_resume_route_validates_decisions_and_returns_thread() -> None:
    valid = {
        "interrupt_id": "interrupt-1",
        "idempotency_key": "resume-request-0001",
        "decisions": [
            {
                "type": "approve",
                "operation_idempotency_key": "send-operation-0001",
            }
        ],
    }
    success = asyncio.run(
        _request(
            _FakeService(),
            "POST",
            f"/api/v1/threads/{THREAD_ID}/resume",
            json=valid,
            headers=_auth(),
        )
    )
    invalid = asyncio.run(
        _request(
            _FakeService(),
            "POST",
            f"/api/v1/threads/{THREAD_ID}/resume",
            json={
                **valid,
                "decisions": [{"type": "edit"}],
            },
            headers=_auth(),
        )
    )

    assert success.status_code == 200
    assert success.json()["data"]["message_count"] == 4
    assert invalid.status_code == 422


def test_stream_chat_returns_named_sse_events() -> None:
    response = asyncio.run(
        _request(
            _FakeService(),
            "POST",
            "/api/v1/chat/stream",
            json=VALID_CHAT,
            headers=_auth(),
        )
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: thread" in response.text
    assert "event: completed" in response.text
    assert '"request_id":"request-123"' in response.text
    assert response.headers["X-Accel-Buffering"] == "no"


def test_stream_error_is_a_safe_sse_event() -> None:
    response = asyncio.run(
        _request(
            _FakeService(error=ProviderTimeoutError("raw secret timeout")),
            "POST",
            "/api/v1/chat/stream",
            json=VALID_CHAT,
            headers=_auth(),
        )
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert '"code":50400' in response.text
    assert "raw secret timeout" not in response.text


def test_openapi_documents_auth_errors_and_sse() -> None:
    response = asyncio.run(_request(_FakeService(), "GET", "/openapi.json"))
    schema = response.json()

    assert "APIKeyHeader" in schema["components"]["securitySchemes"]
    chat = schema["paths"]["/api/v1/chat"]["post"]
    stream = schema["paths"]["/api/v1/chat/stream"]["post"]
    assert set(("400", "401", "403", "404", "409", "422", "429", "503", "504")) <= set(
        chat["responses"]
    )
    assert "text/event-stream" in stream["responses"]["200"]["content"]
