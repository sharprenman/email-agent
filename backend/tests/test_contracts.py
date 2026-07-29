"""邮件与日历领域契约测试。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from email_agent.contracts import (
    CalendarEventInput,
    CalendarProvider,
    EmailSearchCriteria,
    MailProvider,
    ProviderCapabilities,
    ProviderName,
    ProviderRateLimitError,
    SendEmailRequest,
)


def test_provider_capabilities_express_provider_differences() -> None:
    """Gmail 与 Outlook 可以显式声明真实能力差异。"""
    gmail = ProviderCapabilities(provider=ProviderName.GMAIL, unsubscribe_headers=True)
    outlook = ProviderCapabilities(provider=ProviderName.OUTLOOK, unsubscribe_headers=False)

    assert gmail.unsubscribe_headers is True
    assert outlook.unsubscribe_headers is False


def test_calendar_event_requires_valid_aware_time_range() -> None:
    """日历事件拒绝无时区和倒置时间。"""
    start_at = datetime(2026, 7, 21, 9, tzinfo=UTC)

    with pytest.raises(ValidationError, match="结束时间必须晚于开始时间"):
        CalendarEventInput(
            title="无效会议",
            start_at=start_at,
            end_at=start_at - timedelta(minutes=30),
            timezone="Asia/Shanghai",
        )

    with pytest.raises(ValidationError, match="时间必须包含时区"):
        CalendarEventInput(
            title="无时区会议",
            start_at=start_at.replace(tzinfo=None),
            end_at=(start_at + timedelta(hours=1)).replace(tzinfo=None),
            timezone="Asia/Shanghai",
        )


def test_send_email_requires_recipient_subject_and_body() -> None:
    """发送契约在调用 Provider 前拒绝不完整邮件。"""
    with pytest.raises(ValidationError):
        SendEmailRequest(to=(), subject="", body="")


def test_email_search_requires_aware_time_and_unique_keywords() -> None:
    with pytest.raises(ValidationError, match="时区"):
        EmailSearchCriteria(since=datetime(2026, 7, 29, 12))

    with pytest.raises(ValidationError, match="不能重复"):
        EmailSearchCriteria(keywords=("Urgent", "urgent"))


def test_provider_errors_expose_retry_policy() -> None:
    """上层可根据异常类型决定是否安全重试。"""
    error = ProviderRateLimitError("测试限流")

    assert error.code == "provider_rate_limit_error"
    assert error.retryable is True


def test_runtime_protocol_rejects_incomplete_provider() -> None:
    """缺少邮件方法的对象不能被视为完整 Provider。"""

    class IncompleteProvider:
        capabilities = ProviderCapabilities(provider=ProviderName.GMAIL)

    assert not isinstance(IncompleteProvider(), MailProvider)


def test_runtime_protocol_accepts_complete_fake_providers() -> None:
    """Fake Provider 覆盖全部约定方法时可用于后续合约测试。"""
    capabilities = ProviderCapabilities(provider=ProviderName.GMAIL)
    mail = SimpleNamespace(
        capabilities=capabilities,
        **{
            name: AsyncMock()
            for name in (
                "get_identity",
                "read_inbox",
                "search_emails",
                "get_email",
                "get_sent_emails",
                "get_unanswered_emails",
                "list_attachments",
                "download_attachment",
                "list_contacts",
                "send_email",
                "mark_read",
            )
        },
    )
    calendar = SimpleNamespace(
        capabilities=capabilities,
        **{
            name: AsyncMock()
            for name in ("list_events", "create_event", "update_event", "delete_event")
        },
    )

    assert isinstance(mail, MailProvider)
    assert isinstance(calendar, CalendarProvider)
