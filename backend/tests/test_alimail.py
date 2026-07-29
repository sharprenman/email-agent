"""阿里邮箱开放平台邮件与日历 Provider 测试。"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from email_agent.calendar import ApprovalAction, ApprovalService
from email_agent.config import (
    AliMailApiEdition,
    AuthContext,
    Settings,
)
from email_agent.contracts import (
    CalendarEventInput,
    EmailSearchCriteria,
    EmailSearchFolder,
    MailProvider,
    ProviderAuthenticationError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    SendEmailRequest,
)
from email_agent.providers.alimail import (
    ALIMAIL_BASE_URLS,
    AliMailCalendarProvider,
    AliMailClient,
    AliMailProvider,
    build_alimail_providers,
)


def _message(identifier: str, **overrides):
    return {
        "id": identifier,
        "conversationId": f"thread-{identifier}",
        "subject": f"主题 {identifier}",
        "summary": "摘要",
        "from": {"email": "sender@example.com"},
        "toRecipients": [{"email": "owner@example.com"}],
        "sentDateTime": "2026-07-27T08:00:00Z",
        "receivedDateTime": "2026-07-27T08:00:00Z",
        "isRead": False,
        "hasAttachments": False,
        **overrides,
    }


def _client(handler) -> AliMailClient:
    transport = httpx.MockTransport(handler)
    return AliMailClient(
        httpx.AsyncClient(
            base_url=ALIMAIL_BASE_URLS[AliMailApiEdition.STANDARD],
            transport=transport,
        ),
        client_id="client-id",
        client_secret="client-secret",
    )


def _json(request: httpx.Request):
    return json.loads(request.content.decode()) if request.content else None


def test_mail_lists_pages_and_reuses_application_token() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth2/v2.0/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer token"
        cursor = request.url.params.get("cursor")
        if cursor:
            return httpx.Response(
                200,
                json={"messages": [_message("3")], "hasMore": False},
            )
        return httpx.Response(
            200,
            json={
                "messages": [
                    _message("1", isRead=True),
                    _message("2", isRead=False),
                ],
                "nextCursor": "next",
                "hasMore": True,
            },
        )

    provider = AliMailProvider(_client(handler), email="owner@example.com")

    async def run() -> None:
        messages = await provider.read_inbox(limit=2, unread_only=True)
        sent = await provider.get_sent_emails(limit=1)
        assert [item.id for item in messages] == ["2", "3"]
        assert sent[0].id == "1"
        assert isinstance(provider, MailProvider)
        await provider.aclose()

    asyncio.run(run())
    assert sum(request.url.path == "/oauth2/v2.0/token" for request in calls) == 1
    assert any("/mailFolders/2/messages" in request.url.path for request in calls)
    assert any("/mailFolders/1/messages" in request.url.path for request in calls)
    list_request = next(
        request for request in calls if "/mailFolders/2/messages" in request.url.path
    )
    selected_fields = set(list_request.url.params["$select"].split(","))
    assert {
        "id",
        "conversationId",
        "subject",
        "summary",
        "from",
        "sentDateTime",
        "isRead",
        "hasAttachments",
    } <= selected_fields


def test_unanswered_mail_scan_is_bounded_to_latest_page() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth2/v2.0/token":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(
            200,
            json={
                "messages": [_message("1", isReplied=True)],
                "nextCursor": "next",
                "hasMore": True,
            },
        )

    provider = AliMailProvider(_client(handler), email="owner@example.com")

    async def run() -> None:
        assert await provider.get_unanswered_emails(limit=5) == []
        await provider.aclose()

    asyncio.run(run())
    list_calls = [
        request for request in calls if "/mailFolders/2/messages" in request.url.path
    ]
    assert len(list_calls) == 1


def test_mail_search_detail_attachments_contacts_and_download() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth2/v2.0/token":
            return httpx.Response(200, json={"access_token": "token"})
        if path.endswith("/mailFolders/2/messages"):
            return httpx.Response(200, json={"messages": [_message("m1")], "hasMore": False})
        if path.endswith("/messages/m1"):
            return httpx.Response(
                200,
                json={
                    "message": _message(
                        "m1",
                        body={"bodyText": "正文", "bodyHtml": "<p>正文</p>"},
                        internetMessageHeaders={"List-Unsubscribe": "<mailto:x@example.com>"},
                    )
                },
            )
        if path.endswith("/messages/m1/attachments"):
            return httpx.Response(
                200,
                json={
                    "attachments": [
                        {
                            "id": "a1",
                            "name": "报告.pdf",
                            "size": 12,
                            "extHeaders": {"Content-Type": "application/pdf"},
                        }
                    ]
                },
            )
        if path.endswith("/attachments/a1/$value"):
            return httpx.Response(
                302,
                headers={"Location": "/download/a1"},
            )
        if path == "/download/a1":
            return httpx.Response(200, content=b"pdf")
        if path == "/v2/sharedContactFolders/$root/contacts":
            return httpx.Response(
                200,
                json={
                    "contacts": [{"email": "member@example.com", "name": "成员"}],
                    "total": 1,
                },
            )
        raise AssertionError(path)

    provider = AliMailProvider(_client(handler), email="owner@example.com")

    async def run() -> None:
        criteria = EmailSearchCriteria(
            folder=EmailSearchFolder.INBOX,
            query="主题",
        )
        assert (await provider.search_emails(criteria=criteria, limit=1))[0].id == "m1"
        message = await provider.get_email("m1")
        assert message.body_text == "正文"
        assert message.headers["list-unsubscribe"].startswith("<mailto:")
        attachment = (await provider.list_attachments("m1"))[0]
        assert attachment.content_type == "application/pdf"
        assert await provider.download_attachment("m1", "a1") == b"pdf"
        contact = (await provider.list_contacts(limit=1))[0]
        assert contact.display_name == "成员"
        await provider.aclose()

    asyncio.run(run())


def test_mail_reply_send_and_mark_read_use_official_write_endpoints() -> None:
    writes: list[tuple[str, dict | None]] = []
    idempotency_digest = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal idempotency_digest
        path = request.url.path
        if path == "/oauth2/v2.0/token":
            return httpx.Response(200, json={"access_token": "token"})
        if path.endswith("/messages/original"):
            return httpx.Response(
                200,
                json={
                    "message": _message(
                        "original",
                        internetMessageId="<original@example.com>",
                    )
                },
            )
        if path.endswith("/messages") and request.method == "POST":
            writes.append((path, _json(request)))
            idempotency_digest = _json(request)["message"]["internetMessageHeaders"][
                "X-Email-Agent-Idempotency-Key"
            ]
            return httpx.Response(200, json={"message": {"id": "draft-1"}})
        if path.endswith("/messages/draft-1/send"):
            writes.append((path, _json(request)))
            return httpx.Response(200, json={})
        if path.endswith("/mailFolders/1/messages"):
            return httpx.Response(
                200,
                json={
                    "messages": [
                        _message(
                            "sent-1",
                            internetMessageId="<platform-generated@example.com>",
                        )
                    ],
                    "hasMore": False,
                },
            )
        if path.endswith("/messages/sent-1"):
            return httpx.Response(
                200,
                json={
                    "message": _message(
                        "sent-1",
                        internetMessageId="<platform-generated@example.com>",
                        internetMessageHeaders={
                            "X-Email-Agent-Idempotency-Key": idempotency_digest
                        },
                    )
                },
            )
        if path.endswith("/messages/batchUpdate"):
            writes.append((path, _json(request)))
            return httpx.Response(200, json={})
        raise AssertionError((request.method, path))

    provider = AliMailProvider(_client(handler), email="owner@example.com")

    async def run() -> None:
        identifier = await provider.send_email(
            SendEmailRequest(
                to=("to@example.com",),
                subject="回复",
                body="正文",
                reply_to_email_id="original",
            ),
            idempotency_key="mail-idempotency",
        )
        await provider.mark_read("original", idempotency_key="read-idempotency")
        assert identifier == "sent-1"
        await provider.aclose()

    asyncio.run(run())
    draft = writes[0][1]["message"]
    expected_digest = hashlib.sha256(b"mail-idempotency").hexdigest()
    assert draft["internetMessageId"] == f"<email-agent-{expected_digest}@example.com>"
    assert draft["internetMessageHeaders"]["In-Reply-To"] == "<original@example.com>"
    assert writes[1][1] == {"saveToSentItems": True}
    assert writes[2][1]["action"] == "markRead"


@pytest.mark.parametrize(
    ("status", "expected_error"),
    (
        (403, ProviderPermissionError),
        (429, ProviderRateLimitError),
        (504, ProviderTimeoutError),
    ),
)
def test_client_maps_provider_errors(status, expected_error) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/v2.0/token":
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(status, json={"message": "provider error"})

    client = _client(handler)

    async def run() -> None:
        with pytest.raises(expected_error):
            await client.request("GET", "/v2/test")
        await client.aclose()

    asyncio.run(run())


def test_client_maps_network_timeout_after_read_retries() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/oauth2/v2.0/token":
            return httpx.Response(200, json={"access_token": "token"})
        attempts += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = _client(handler)

    async def run() -> None:
        with pytest.raises(ProviderTimeoutError):
            await client.request("GET", "/v2/test")
        await client.aclose()

    asyncio.run(run())
    assert attempts == 3


def test_calendar_crud_consumes_approval_and_uses_selected_calendar() -> None:
    writes: list[tuple[str, str, dict | None]] = []
    event_input = CalendarEventInput(
        title="项目会议",
        start_at=datetime(2026, 7, 28, 1, tzinfo=UTC),
        end_at=datetime(2026, 7, 28, 2, tzinfo=UTC),
        timezone="Asia/Shanghai",
        attendees=("member@example.com",),
        location="会议室",
        description="讨论项目",
    )
    raw_event = {
        "id": "event-1",
        "subject": "项目会议",
        "startUtc": {"time": "2026-07-28T01:00:00Z"},
        "endUtc": {"time": "2026-07-28T02:00:00Z"},
        "start": {"timezone": "Asia/Shanghai"},
        "attendees": [{"user": {"email": "member@example.com"}}],
        "locations": [{"name": "会议室"}],
        "body": {"bodyText": "讨论项目"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth2/v2.0/token":
            return httpx.Response(200, json={"access_token": "token"})
        if path.endswith("/calendars"):
            return httpx.Response(
                200,
                json={
                    "calendars": [
                        {"id": "other", "isSelected": False},
                        {"id": "primary", "isSelected": True},
                    ]
                },
            )
        if path.endswith("/eventsview"):
            return httpx.Response(200, json={"events": [raw_event], "hasMore": False})
        if path.endswith("/events") and request.method == "POST":
            writes.append((request.method, path, _json(request)))
            return httpx.Response(200, json={"eventId": "event-1"})
        if path.endswith("/events/event-1") and request.method == "GET":
            return httpx.Response(200, json={"event": raw_event})
        if path.endswith("/events/event-1"):
            writes.append((request.method, path, _json(request)))
            return httpx.Response(200, json={})
        raise AssertionError((request.method, path))

    approvals = ApprovalService("a" * 32)
    auth = AuthContext(user_id="local-user")
    provider = AliMailCalendarProvider(
        _client(handler),
        email="owner@example.com",
        approvals=approvals,
    )

    def token(action: ApprovalAction, target: str | None, payload: dict, key: str) -> str:
        return approvals.mint_after_interrupt(
            auth,
            action=action,
            target_id=target,
            payload=payload,
            idempotency_key=key,
        )

    async def run() -> None:
        events = await provider.list_events(
            start_at=datetime(2026, 7, 1, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        assert events[0].id == "event-1"
        payload = event_input.model_dump(mode="json")
        created = await provider.create_event(
            event_input,
            user_id=auth.user_id,
            approval_token=token(ApprovalAction.CREATE, None, payload, "create-key"),
            idempotency_key="create-key",
        )
        assert created.id == "event-1"
        await provider.update_event(
            "event-1",
            event_input,
            user_id=auth.user_id,
            approval_token=token(
                ApprovalAction.UPDATE,
                "event-1",
                payload,
                "update-key",
            ),
            idempotency_key="update-key",
        )
        await provider.delete_event(
            "event-1",
            user_id=auth.user_id,
            approval_token=token(
                ApprovalAction.DELETE,
                "event-1",
                {},
                "delete-key",
            ),
            idempotency_key="delete-key",
        )

    asyncio.run(run())
    assert all("/calendars/primary/" in path for _, path, _ in writes)
    assert [method for method, _, _ in writes] == ["POST", "POST", "DELETE"]
    assert writes[0][2]["notify"] is True


def test_builder_requires_credentials_and_selects_localized_host(monkeypatch) -> None:
    with pytest.raises(ProviderAuthenticationError, match="ALIMAIL_CLIENT_ID"):
        build_alimail_providers(Settings(), ApprovalService("a" * 32))

    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("email_agent.providers.alimail.httpx.AsyncClient", FakeAsyncClient)
    mail, calendar = build_alimail_providers(
        Settings(
            alimail_api_edition=AliMailApiEdition.LOCALIZED,
            alimail_client_id="client-id",
            alimail_client_secret="client-secret",
            alimail_account_email="owner@example.com",
        ),
        ApprovalService("a" * 32),
    )

    assert captured["base_url"] == ALIMAIL_BASE_URLS[AliMailApiEdition.LOCALIZED]
    assert mail.capabilities.provider.value == "alimail"
    assert calendar.capabilities.calendar is True
