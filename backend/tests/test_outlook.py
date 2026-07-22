"""Outlook Provider 的确定性单元测试。"""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from email_agent.config import Settings
from email_agent.contracts import (
    MailProvider,
    ProviderAuthenticationError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SendEmailRequest,
)
from email_agent.outlook import GRAPH_BASE_URL, OutlookProvider, build_outlook_provider


def _message(
    email_id: str,
    *,
    sender: str = "sender@example.com",
    conversation_id: str | None = None,
    received_at: str = "2026-07-22T08:00:00Z",
    unread: bool = False,
    attachment: bool = False,
):
    return {
        "id": email_id,
        "conversationId": conversation_id or f"conversation-{email_id}",
        "subject": "测试主题",
        "from": {"emailAddress": {"name": "发件人", "address": sender}},
        "toRecipients": [{"emailAddress": {"address": "me@example.com"}}],
        "ccRecipients": [{"emailAddress": {"address": "copy@example.com"}}],
        "receivedDateTime": received_at,
        "bodyPreview": "摘要",
        "isRead": not unread,
        "hasAttachments": attachment,
        "body": {"contentType": "html", "content": "<p>邮件正文</p>"},
        "internetMessageHeaders": [{"name": "Message-ID", "value": f"<{email_id}>"}],
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=GRAPH_BASE_URL, transport=httpx.MockTransport(handler))


def _ok(request: httpx.Request, payload=None, *, status: int = 200) -> httpx.Response:
    if payload is None:
        return httpx.Response(status, request=request)
    return httpx.Response(status, request=request, json=payload)


def test_identity_lists_search_pagination_and_capabilities() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/me"):
            return _ok(
                request,
                {
                    "displayName": "本地用户",
                    "mail": None,
                    "userPrincipalName": "me@example.com",
                },
            )
        if path.endswith("/mailFolders/inbox/messages"):
            return _ok(
                request,
                {
                    "value": [_message("inbox-1", unread=True)],
                    "@odata.nextLink": f"{GRAPH_BASE_URL}/page-2",
                },
            )
        if path.endswith("/page-2"):
            return _ok(request, {"value": [_message("inbox-2")]})
        if path.endswith("/mailFolders/sentitems/messages"):
            return _ok(request, {"value": [_message("sent-1", sender="me@example.com")]})
        if path.endswith("/me/messages"):
            return _ok(request, {"value": [_message("search-1")]})
        raise AssertionError(f"未处理的请求：{request.url}")

    async def scenario() -> None:
        provider = OutlookProvider(_client(handler), access_token="access-token")
        identity = await provider.get_identity()
        inbox = await provider.read_inbox(limit=2, unread_only=True)
        searched = await provider.search_emails(query="项目进展", limit=1)
        sent = await provider.get_sent_emails(limit=1)

        assert (identity.email, identity.display_name) == ("me@example.com", "本地用户")
        assert [item.id for item in inbox] == ["inbox-1", "inbox-2"]
        assert searched[0].id == "search-1"
        assert sent[0].id == "sent-1"
        assert isinstance(provider, MailProvider)
        assert provider.capabilities.unsubscribe_headers is False
        await provider.aclose()

    asyncio.run(scenario())
    inbox_request = next(
        request for request in requests if request.url.path.endswith("/mailFolders/inbox/messages")
    )
    search_request = next(
        request
        for request in requests
        if request.url.path.endswith("/me/messages") and "$search" in request.url.params
    )
    assert inbox_request.url.params["$filter"] == "isRead eq false"
    assert "$orderby" not in inbox_request.url.params
    assert search_request.headers["ConsistencyLevel"] == "eventual"
    assert len(requests[2].url.params) == 0


def test_full_message_attachments_contacts_and_empty_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/messages/mail-1"):
            return _ok(request, _message("mail-1", attachment=True))
        if path.endswith("/messages/mail-1/attachments"):
            return _ok(
                request,
                {
                    "value": [
                        {
                            "id": "attachment-1",
                            "name": "报告.pdf",
                            "contentType": "application/pdf",
                            "size": 128,
                        }
                    ]
                },
            )
        if path.endswith("/contacts"):
            return _ok(
                request,
                {
                    "value": [
                        {
                            "displayName": "张三",
                            "emailAddresses": [{"address": "zhangsan@example.com"}],
                        },
                        {"displayName": "无邮箱", "emailAddresses": []},
                    ]
                },
            )
        if path.endswith("/mailFolders/inbox/messages"):
            return _ok(request, {"value": []})
        raise AssertionError(f"未处理的请求：{request.url}")

    async def scenario() -> None:
        provider = OutlookProvider(_client(handler), access_token="access-token")
        email = await provider.get_email("mail-1")
        attachments = await provider.list_attachments("mail-1")
        contacts = await provider.list_contacts(limit=10)

        assert email.body_html == "<p>邮件正文</p>"
        assert email.body_text == ""
        assert email.recipients == ("me@example.com", "copy@example.com")
        assert email.headers["message-id"] == "<mail-1>"
        assert attachments[0].filename == "报告.pdf"
        assert (contacts[0].display_name, contacts[0].email) == (
            "张三",
            "zhangsan@example.com",
        )
        assert await provider.read_inbox(limit=10) == []
        await provider.aclose()

    asyncio.run(scenario())


def test_unanswered_only_keeps_external_latest_sender() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me"):
            return _ok(request, {"mail": "me@example.com"})
        if request.url.path.endswith("/mailFolders/inbox/messages"):
            return _ok(
                request,
                {
                    "value": [
                        _message("candidate-1", conversation_id="waiting"),
                        _message("candidate-2", conversation_id="answered"),
                    ]
                },
            )
        conversation_filter = request.url.params.get("$filter", "")
        if "waiting" in conversation_filter:
            return _ok(
                request,
                {
                    "value": [
                        _message(
                            "waiting-old",
                            conversation_id="waiting",
                            received_at="2026-07-21T08:00:00Z",
                        ),
                        _message("waiting-new", conversation_id="waiting"),
                    ]
                },
            )
        if "answered" in conversation_filter:
            return _ok(
                request,
                {
                    "value": [
                        _message(
                            "answered-new",
                            sender="me@example.com",
                            conversation_id="answered",
                        )
                    ]
                },
            )
        raise AssertionError(f"未处理的请求：{request.url}")

    async def scenario() -> None:
        provider = OutlookProvider(_client(handler), access_token="access-token")
        result = await provider.get_unanswered_emails(limit=2)
        assert [item.id for item in result] == ["waiting-new"]
        await provider.aclose()

    asyncio.run(scenario())


def test_create_send_reply_and_mark_read() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/me/messages"):
            return _ok(request, {"id": "draft-new"}, status=201)
        if request.method == "POST" and path.endswith("/original/createReply"):
            return _ok(request, {"id": "draft-reply"}, status=201)
        if request.method == "POST" and path.endswith("/send"):
            return _ok(request, status=202)
        if request.method == "PATCH":
            return _ok(request, {"id": path.rsplit("/", 1)[-1]})
        raise AssertionError(f"未处理的请求：{request.method} {request.url}")

    async def scenario() -> None:
        provider = OutlookProvider(_client(handler), access_token="access-token")
        new_request = SendEmailRequest(
            to=("receiver@example.com",),
            subject="新邮件",
            body="正文",
            cc=("copy@example.com",),
        )
        reply_request = SendEmailRequest(
            to=("receiver@example.com",),
            subject="回复邮件",
            body="回复正文",
            reply_to_email_id="original",
        )

        assert await provider.send_email(new_request, idempotency_key="new-1") == "draft-new"
        assert await provider.send_email(reply_request, idempotency_key="reply-1") == "draft-reply"
        await provider.mark_read("original", idempotency_key="read-1")
        await provider.aclose()

    asyncio.run(scenario())
    create_payload = json.loads(requests[0].content)
    reply_patch = next(
        request
        for request in requests
        if request.method == "PATCH" and request.url.path.endswith("/draft-reply")
    )
    mark_read = next(
        request
        for request in requests
        if request.method == "PATCH" and request.url.path.endswith("/original")
    )
    assert create_payload["internetMessageHeaders"][0]["name"].startswith("X-")
    assert "internetMessageHeaders" not in json.loads(reply_patch.content)
    assert json.loads(mark_read.content) == {"isRead": True}


def test_expired_access_token_is_refreshed_once() -> None:
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return _ok(
                request,
                {"access_token": "new-token", "refresh_token": "rotated-token"},
            )
        authorizations.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old-token":
            return _ok(request, {"error": "expired"}, status=401)
        return _ok(request, {"mail": "me@example.com"})

    async def scenario() -> None:
        provider = OutlookProvider(
            _client(handler),
            access_token="old-token",
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            refresh_token="refresh",
        )
        assert (await provider.get_identity()).email == "me@example.com"
        await provider.aclose()

    asyncio.run(scenario())
    assert authorizations == ["Bearer old-token", "Bearer new-token"]


def test_safe_reads_retry_but_writes_do_not() -> None:
    read_calls = 0
    write_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal read_calls, write_calls
        if request.method == "GET":
            read_calls += 1
            if read_calls == 1:
                return _ok(request, {"error": "temporary"}, status=503)
            return _ok(request, {"mail": "me@example.com"})
        write_calls += 1
        return _ok(request, {"error": "temporary"}, status=503)

    async def scenario() -> None:
        provider = OutlookProvider(_client(handler), access_token="access-token")
        assert (await provider.get_identity()).email == "me@example.com"
        request = SendEmailRequest(
            to=("receiver@example.com",),
            subject="测试邮件",
            body="正文",
        )
        with pytest.raises(ProviderUnavailableError):
            await provider.send_email(request, idempotency_key="send-1")
        await provider.aclose()

    asyncio.run(scenario())
    assert read_calls == 2
    assert write_calls == 1


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (401, ProviderAuthenticationError),
        (403, ProviderPermissionError),
        (404, ProviderNotFoundError),
        (429, ProviderRateLimitError),
        (504, ProviderTimeoutError),
        (500, ProviderUnavailableError),
    ],
)
def test_graph_http_errors_are_mapped(status: int, expected_error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(request, {"error": "Graph error"}, status=status)

    async def scenario() -> None:
        provider = OutlookProvider(
            _client(handler),
            access_token="access-token",
            read_retries=0,
        )
        with pytest.raises(expected_error):
            await provider.get_identity()
        await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("source_error", "expected_error"),
    [
        (httpx.ReadTimeout("请求超时"), ProviderTimeoutError),
        (httpx.ConnectError("连接失败"), ProviderUnavailableError),
    ],
)
def test_graph_transport_errors_are_mapped(
    source_error: Exception, expected_error: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise source_error

    async def scenario() -> None:
        provider = OutlookProvider(
            _client(handler),
            access_token="access-token",
            read_retries=0,
        )
        with pytest.raises(expected_error):
            await provider.get_identity()
        await provider.aclose()

    asyncio.run(scenario())


def test_factory_rejects_missing_oauth_configuration() -> None:
    with pytest.raises(ProviderAuthenticationError, match="MICROSOFT_CLIENT_ID"):
        build_outlook_provider(Settings())
