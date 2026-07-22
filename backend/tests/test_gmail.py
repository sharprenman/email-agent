"""Gmail Provider 的确定性单元测试。"""

import asyncio
import base64
from email import policy
from email.parser import BytesParser
from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError
from httplib2 import Response

from email_agent.config import Settings
from email_agent.contracts import (
    ProviderAuthenticationError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SendEmailRequest,
    UnsupportedCapabilityError,
)
from email_agent.gmail import GmailProvider, build_gmail_provider


class FakeRequest:
    """模拟 Google 客户端的 execute 请求对象。"""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result or {}
        self.error = error
        self.retries: list[int] = []

    def execute(self, *, num_retries: int = 0):
        self.retries.append(num_retries)
        if self.error:
            raise self.error
        return self.result


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _message(
    email_id: str,
    *,
    sender: str = "sender@example.com",
    timestamp: int = 1_700_000_000_000,
    unread: bool = False,
    attachment: bool = False,
):
    parts = [
        {"mimeType": "text/plain", "body": {"data": _encoded("纯文本正文")}},
        {"mimeType": "text/html", "body": {"data": _encoded("<p>正文</p>")}},
    ]
    if attachment:
        parts.append(
            {
                "filename": "报告.pdf",
                "mimeType": "application/pdf",
                "body": {"attachmentId": "attachment-1", "size": 128},
            }
        )
    return {
        "id": email_id,
        "threadId": f"thread-{email_id}",
        "internalDate": str(timestamp),
        "snippet": "摘要",
        "labelIds": ["INBOX", *(["UNREAD"] if unread else [])],
        "payload": {
            "headers": [
                {"name": "From", "value": f"发件人 <{sender}>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Cc", "value": "copy@example.com"},
                {"name": "Subject", "value": "测试主题"},
                {"name": "Message-ID", "value": f"<{email_id}@example.com>"},
            ],
            "parts": parts,
        },
    }


def _gmail_mocks():
    gmail = MagicMock()
    users = gmail.users.return_value
    return gmail, users.messages.return_value, users.threads.return_value, users


def test_list_identity_inbox_search_and_sent() -> None:
    gmail, messages, _, users = _gmail_mocks()
    users.getProfile.return_value = FakeRequest({"emailAddress": "me@example.com"})
    messages.list.return_value = FakeRequest({"messages": [{"id": "mail-1"}]})
    messages.get.return_value = FakeRequest(_message("mail-1", unread=True))
    provider = GmailProvider(gmail)

    assert asyncio.run(provider.get_identity()).email == "me@example.com"
    assert asyncio.run(provider.read_inbox(limit=2, unread_only=True))[0].is_read is False
    assert messages.list.call_args.kwargs["labelIds"] == ["INBOX"]
    assert messages.list.call_args.kwargs["q"] == "is:unread"
    assert len(asyncio.run(provider.search_emails(query="from:sender", limit=2))) == 1
    assert messages.list.call_args.kwargs["q"] == "from:sender"
    assert len(asyncio.run(provider.get_sent_emails(limit=2))) == 1
    assert messages.list.call_args.kwargs["labelIds"] == ["SENT"]


def test_full_message_body_and_attachments() -> None:
    gmail, messages, _, _ = _gmail_mocks()
    messages.get.return_value = FakeRequest(_message("mail-1", attachment=True))
    provider = GmailProvider(gmail)

    email = asyncio.run(provider.get_email("mail-1"))
    attachments = asyncio.run(provider.list_attachments("mail-1"))

    assert email.body_text == "纯文本正文"
    assert email.body_html == "<p>正文</p>"
    assert email.recipients == ("me@example.com", "copy@example.com")
    assert email.has_attachments is True
    assert attachments[0].filename == "报告.pdf"


def test_message_list_follows_page_token_until_limit() -> None:
    gmail, messages, _, _ = _gmail_mocks()
    messages.list.side_effect = [
        FakeRequest({"messages": [{"id": "mail-1"}], "nextPageToken": "next"}),
        FakeRequest({"messages": [{"id": "mail-2"}]}),
    ]
    messages.get.side_effect = [
        FakeRequest(_message("mail-1")),
        FakeRequest(_message("mail-2")),
    ]

    result = asyncio.run(GmailProvider(gmail).read_inbox(limit=2))

    assert [item.id for item in result] == ["mail-1", "mail-2"]
    assert messages.list.call_args_list[1].kwargs["pageToken"] == "next"


def test_empty_message_list_returns_empty_result() -> None:
    gmail, messages, _, _ = _gmail_mocks()
    messages.list.return_value = FakeRequest({})

    assert asyncio.run(GmailProvider(gmail).read_inbox(limit=10)) == []


def test_unanswered_threads_only_keep_external_latest_sender() -> None:
    gmail, messages, threads, users = _gmail_mocks()
    users.getProfile.return_value = FakeRequest({"emailAddress": "me@example.com"})
    messages.list.return_value = FakeRequest(
        {"messages": [{"threadId": "waiting"}, {"threadId": "answered"}]}
    )
    threads.get.side_effect = [
        FakeRequest({"messages": [_message("old"), _message("new", timestamp=1_800_000_000_000)]}),
        FakeRequest({"messages": [_message("mine", sender="me@example.com")]}),
    ]

    result = asyncio.run(GmailProvider(gmail).get_unanswered_emails(limit=2))

    assert [item.id for item in result] == ["new"]


def test_contacts_and_missing_people_capability() -> None:
    gmail, _, _, _ = _gmail_mocks()
    people = MagicMock()
    people.people.return_value.connections.return_value.list.return_value = FakeRequest(
        {
            "connections": [
                {
                    "names": [{"displayName": "张三", "metadata": {"primary": True}}],
                    "emailAddresses": [
                        {"value": "zhangsan@example.com", "metadata": {"primary": True}}
                    ],
                }
            ]
        }
    )

    contact = asyncio.run(GmailProvider(gmail, people).list_contacts(limit=10))[0]
    assert (contact.display_name, contact.email) == ("张三", "zhangsan@example.com")
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(GmailProvider(gmail).list_contacts(limit=10))


def test_reply_send_and_mark_read() -> None:
    gmail, messages, _, _ = _gmail_mocks()
    messages.get.return_value = FakeRequest(_message("original"))
    messages.send.return_value = FakeRequest({"id": "sent-1"})
    messages.modify.return_value = FakeRequest({"id": "original"})
    request = SendEmailRequest(
        to=("receiver@example.com",),
        subject="回复主题",
        body="回复正文",
        reply_to_email_id="original",
    )
    provider = GmailProvider(gmail)

    assert asyncio.run(provider.send_email(request, idempotency_key="request-1")) == "sent-1"
    body = messages.send.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(body["raw"])
    mime = BytesParser(policy=policy.default).parsebytes(raw)
    assert body["threadId"] == "thread-original"
    assert mime["In-Reply-To"] == "<original@example.com>"
    assert mime["X-Email-Agent-Idempotency-Key"]

    asyncio.run(provider.mark_read("original", idempotency_key="request-2"))
    assert messages.modify.call_args.kwargs["body"] == {"removeLabelIds": ["UNREAD"]}


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
def test_google_http_errors_are_mapped(status: int, expected_error: type[Exception]) -> None:
    gmail, _, _, users = _gmail_mocks()
    error = HttpError(Response({"status": str(status)}), b"Google error")
    users.getProfile.return_value = FakeRequest(error=error)

    with pytest.raises(expected_error):
        asyncio.run(GmailProvider(gmail).get_identity())


@pytest.mark.parametrize(
    ("source_error", "expected_error"),
    [
        (RefreshError("token 已失效"), ProviderAuthenticationError),
        (TimeoutError(), ProviderTimeoutError),
        (TransportError("连接失败"), ProviderUnavailableError),
    ],
)
def test_google_client_errors_are_mapped(
    source_error: Exception, expected_error: type[Exception]
) -> None:
    gmail, _, _, users = _gmail_mocks()
    users.getProfile.return_value = FakeRequest(error=source_error)

    with pytest.raises(expected_error):
        asyncio.run(GmailProvider(gmail).get_identity())


def test_factory_rejects_missing_oauth_configuration() -> None:
    with pytest.raises(ProviderAuthenticationError, match="GOOGLE_CLIENT_ID"):
        build_gmail_provider(Settings())
