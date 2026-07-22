"""双日历 Provider 与一次性审批边界测试。"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response
from pydantic import ValidationError

from email_agent.calendar import (
    ApprovalAction,
    ApprovalConsumedError,
    ApprovalExpiredError,
    ApprovalMismatchError,
    ApprovalRequiredError,
    ApprovalService,
    GoogleCalendarProvider,
    MicrosoftCalendarProvider,
    build_approval_service,
    build_google_calendar_provider,
)
from email_agent.config import AppEnvironment, AuthContext, Settings
from email_agent.contracts import (
    CalendarEventInput,
    CalendarProvider,
    CalendarRecurrence,
    ProviderAuthenticationError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RecurrenceFrequency,
    RecurrenceWeekday,
)


class FakeRequest:
    """模拟 Google API execute 请求对象。"""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.retries: list[int] = []

    def execute(self, *, num_retries: int = 0):
        self.retries.append(num_retries)
        if self.error:
            raise self.error
        return self.result


class FakeGraph:
    """记录 Microsoft Graph 日历调用的最小请求器。"""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict]] = []
        self.closed = False

    async def request_graph(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)

    async def aclose(self) -> None:
        self.closed = True


def _event(*, title: str = "项目会议") -> CalendarEventInput:
    return CalendarEventInput(
        title=title,
        start_at=datetime(2026, 7, 23, 1, tzinfo=UTC),
        end_at=datetime(2026, 7, 23, 2, tzinfo=UTC),
        timezone="Asia/Shanghai",
        attendees=("member@example.com",),
        location="会议室 A",
        description="讨论项目进度",
        recurrence=CalendarRecurrence(
            frequency=RecurrenceFrequency.WEEKLY,
            count=3,
            weekdays=(RecurrenceWeekday.THURSDAY,),
        ),
    )


def _google_event(event_id: str = "google-event") -> dict:
    return {
        "id": event_id,
        "summary": "项目会议",
        "start": {"dateTime": "2026-07-23T09:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": "2026-07-23T10:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "attendees": [{"email": "member@example.com"}],
        "location": "会议室 A",
        "description": "讨论项目进度",
        "recurrence": ["RRULE:FREQ=WEEKLY;INTERVAL=1;COUNT=3;BYDAY=TH"],
    }


def _microsoft_event(event_id: str = "microsoft-event") -> dict:
    return {
        "id": event_id,
        "subject": "项目会议",
        "start": {"dateTime": "2026-07-23T09:00:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": "2026-07-23T10:00:00", "timeZone": "Asia/Shanghai"},
        "attendees": [{"emailAddress": {"address": "member@example.com"}, "type": "required"}],
        "location": {"displayName": "会议室 A"},
        "body": {"contentType": "text", "content": "讨论项目进度"},
        "recurrence": {
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["thursday"]},
            "range": {
                "type": "numbered",
                "startDate": "2026-07-23",
                "numberOfOccurrences": 3,
                "recurrenceTimeZone": "Asia/Shanghai",
            },
        },
    }


def _approval(
    service: ApprovalService,
    event: CalendarEventInput | None,
    *,
    action: ApprovalAction,
    target_id: str | None,
    idempotency_key: str,
    ttl_seconds: int = 300,
) -> str:
    return service.mint_after_interrupt(
        AuthContext(user_id="private-owner"),
        action=action,
        target_id=target_id,
        payload=event.model_dump(mode="json") if event else {},
        idempotency_key=idempotency_key,
        ttl_seconds=ttl_seconds,
    )


def test_calendar_contract_validates_timezone_attendees_and_recurrence() -> None:
    with pytest.raises(ValidationError, match="IANA 时区"):
        CalendarEventInput(
            title="无效时区",
            start_at=datetime(2026, 7, 23, 1, tzinfo=UTC),
            end_at=datetime(2026, 7, 23, 2, tzinfo=UTC),
            timezone="Not/A-Timezone",
        )
    with pytest.raises(ValidationError, match="重复邮箱"):
        CalendarEventInput(
            title="重复参与者",
            start_at=datetime(2026, 7, 23, 1, tzinfo=UTC),
            end_at=datetime(2026, 7, 23, 2, tzinfo=UTC),
            timezone="UTC",
            attendees=("same@example.com", "SAME@example.com"),
        )
    with pytest.raises(ValidationError, match="有效邮箱"):
        CalendarEventInput(
            title="无效参与者",
            start_at=datetime(2026, 7, 23, 1, tzinfo=UTC),
            end_at=datetime(2026, 7, 23, 2, tzinfo=UTC),
            timezone="UTC",
            attendees=("@example.com",),
        )
    with pytest.raises(ValidationError, match="不能同时设置"):
        CalendarRecurrence(
            frequency=RecurrenceFrequency.DAILY,
            count=2,
            until=date(2026, 8, 1),
        )
    with pytest.raises(ValidationError, match="结束日期不能早于"):
        CalendarEventInput(
            title="过期重复规则",
            start_at=datetime(2026, 7, 23, 1, tzinfo=UTC),
            end_at=datetime(2026, 7, 23, 2, tzinfo=UTC),
            timezone="UTC",
            recurrence=CalendarRecurrence(
                frequency=RecurrenceFrequency.DAILY,
                until=date(2026, 7, 22),
            ),
        )


def test_approval_rejects_missing_tampered_mismatched_expired_and_reused_tokens() -> None:
    now = [datetime(2026, 7, 22, 8, tzinfo=UTC)]
    service = ApprovalService("a" * 32, clock=lambda: now[0])
    event = _event()
    token = _approval(
        service,
        event,
        action=ApprovalAction.CREATE,
        target_id=None,
        idempotency_key="create-1",
        ttl_seconds=10,
    )

    with pytest.raises(ApprovalRequiredError):
        service.consume(
            "",
            user_id="private-owner",
            action=ApprovalAction.CREATE,
            target_id=None,
            payload=event.model_dump(mode="json"),
            idempotency_key="create-1",
        )
    with pytest.raises(ApprovalRequiredError):
        service.consume(
            token + "tampered",
            user_id="private-owner",
            action=ApprovalAction.CREATE,
            target_id=None,
            payload=event.model_dump(mode="json"),
            idempotency_key="create-1",
        )
    with pytest.raises(ApprovalMismatchError):
        service.consume(
            token,
            user_id="another-user",
            action=ApprovalAction.CREATE,
            target_id=None,
            payload=event.model_dump(mode="json"),
            idempotency_key="create-1",
        )

    service.consume(
        token,
        user_id="private-owner",
        action=ApprovalAction.CREATE,
        target_id=None,
        payload=event.model_dump(mode="json"),
        idempotency_key="create-1",
    )
    with pytest.raises(ApprovalConsumedError):
        service.consume(
            token,
            user_id="private-owner",
            action=ApprovalAction.CREATE,
            target_id=None,
            payload=event.model_dump(mode="json"),
            idempotency_key="create-1",
        )

    expired = _approval(
        service,
        event,
        action=ApprovalAction.CREATE,
        target_id=None,
        idempotency_key="create-2",
        ttl_seconds=1,
    )
    now[0] += timedelta(seconds=1)
    with pytest.raises(ApprovalExpiredError):
        service.consume(
            expired,
            user_id="private-owner",
            action=ApprovalAction.CREATE,
            target_id=None,
            payload=event.model_dump(mode="json"),
            idempotency_key="create-2",
        )


def test_google_calendar_list_writes_and_one_time_approval() -> None:
    service = MagicMock()
    events_api = service.events.return_value
    events_api.list.side_effect = [
        FakeRequest({"items": [_google_event("page-1")], "nextPageToken": "next"}),
        FakeRequest({"items": [_google_event("page-2")]}),
    ]
    events_api.insert.return_value = FakeRequest(_google_event("created"))
    events_api.update.return_value = FakeRequest(_google_event("updated"))
    events_api.delete.return_value = FakeRequest(None)
    approvals = ApprovalService("b" * 32)
    provider = GoogleCalendarProvider(service, approvals)
    event = _event()

    async def scenario() -> None:
        listed = await provider.list_events(
            start_at=datetime(2026, 7, 1, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        assert [item.id for item in listed] == ["page-1", "page-2"]
        create_token = _approval(
            approvals,
            event,
            action=ApprovalAction.CREATE,
            target_id=None,
            idempotency_key="google-create",
        )
        created = await provider.create_event(
            event,
            user_id="private-owner",
            approval_token=create_token,
            idempotency_key="google-create",
        )
        with pytest.raises(ApprovalConsumedError):
            await provider.create_event(
                event,
                user_id="private-owner",
                approval_token=create_token,
                idempotency_key="google-create",
            )
        update_token = _approval(
            approvals,
            event,
            action=ApprovalAction.UPDATE,
            target_id="created",
            idempotency_key="google-update",
        )
        updated = await provider.update_event(
            "created",
            event,
            user_id="private-owner",
            approval_token=update_token,
            idempotency_key="google-update",
        )
        delete_token = _approval(
            approvals,
            None,
            action=ApprovalAction.DELETE,
            target_id="created",
            idempotency_key="google-delete",
        )
        await provider.delete_event(
            "created",
            user_id="private-owner",
            approval_token=delete_token,
            idempotency_key="google-delete",
        )
        assert (created.id, updated.id) == ("created", "updated")

    asyncio.run(scenario())
    assert events_api.list.call_args_list[1].kwargs["pageToken"] == "next"
    insert_body = events_api.insert.call_args.kwargs["body"]
    assert insert_body["id"].startswith("ea")
    assert insert_body["recurrence"] == ["RRULE:FREQ=WEEKLY;INTERVAL=1;COUNT=3;BYDAY=TH"]
    assert events_api.insert.call_count == 1
    assert events_api.insert.call_args.kwargs["sendUpdates"] == "all"
    assert events_api.update.call_args.kwargs["eventId"] == "created"
    assert events_api.delete.call_args.kwargs["eventId"] == "created"


def test_google_calendar_rejects_changed_approved_payload_before_api_call() -> None:
    service = MagicMock()
    approvals = ApprovalService("c" * 32)
    provider = GoogleCalendarProvider(service, approvals)
    approved_event = _event()
    changed_event = _event(title="被篡改的标题")
    token = _approval(
        approvals,
        approved_event,
        action=ApprovalAction.CREATE,
        target_id=None,
        idempotency_key="create-1",
    )

    with pytest.raises(ApprovalMismatchError):
        asyncio.run(
            provider.create_event(
                changed_event,
                user_id="private-owner",
                approval_token=token,
                idempotency_key="create-1",
            )
        )
    service.events.return_value.insert.assert_not_called()


def test_microsoft_calendar_list_writes_and_payload_mapping() -> None:
    def handler(method: str, url: str, kwargs: dict):
        if method == "GET" and url == "/me/calendarView":
            return {
                "value": [_microsoft_event("page-1")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/page-2",
            }
        if method == "GET" and url.endswith("/page-2"):
            return {"value": [_microsoft_event("page-2")]}
        if method == "POST":
            return _microsoft_event("created")
        if method == "PATCH":
            return _microsoft_event("updated")
        if method == "DELETE":
            return {}
        raise AssertionError(f"未处理的请求：{method} {url} {kwargs}")

    graph = FakeGraph(handler)
    approvals = ApprovalService("d" * 32)
    provider = MicrosoftCalendarProvider(graph, approvals)
    event = _event()

    async def scenario() -> None:
        listed = await provider.list_events(
            start_at=datetime(2026, 7, 1, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        create_token = _approval(
            approvals,
            event,
            action=ApprovalAction.CREATE,
            target_id=None,
            idempotency_key="ms-create",
        )
        created = await provider.create_event(
            event,
            user_id="private-owner",
            approval_token=create_token,
            idempotency_key="ms-create",
        )
        update_token = _approval(
            approvals,
            event,
            action=ApprovalAction.UPDATE,
            target_id="created",
            idempotency_key="ms-update",
        )
        updated = await provider.update_event(
            "created",
            event,
            user_id="private-owner",
            approval_token=update_token,
            idempotency_key="ms-update",
        )
        delete_token = _approval(
            approvals,
            None,
            action=ApprovalAction.DELETE,
            target_id="created",
            idempotency_key="ms-delete",
        )
        await provider.delete_event(
            "created",
            user_id="private-owner",
            approval_token=delete_token,
            idempotency_key="ms-delete",
        )
        assert [item.id for item in listed] == ["page-1", "page-2"]
        assert (created.id, updated.id) == ("created", "updated")
        assert isinstance(provider, CalendarProvider)
        await provider.aclose()

    asyncio.run(scenario())
    assert graph.calls[1][2]["params"] is None
    create_call = next(call for call in graph.calls if call[0] == "POST")
    create_payload = create_call[2]["json"]
    assert create_payload["transactionId"]
    assert create_payload["start"]["timeZone"] == "Asia/Shanghai"
    assert create_payload["recurrence"]["pattern"]["daysOfWeek"] == ["thursday"]
    assert create_call[2]["retry_read"] is False
    assert graph.closed is True


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (403, ProviderPermissionError),
        (404, ProviderNotFoundError),
        (429, ProviderRateLimitError),
        (504, ProviderTimeoutError),
        (500, ProviderUnavailableError),
    ],
)
def test_google_calendar_errors_are_mapped(status: int, expected_error: type[Exception]) -> None:
    service = MagicMock()
    error = HttpError(Response({"status": str(status)}), b"Calendar error")
    service.events.return_value.list.return_value = FakeRequest(error=error)
    provider = GoogleCalendarProvider(service, ApprovalService("e" * 32))

    with pytest.raises(expected_error):
        asyncio.run(
            provider.list_events(
                start_at=datetime(2026, 7, 1, tzinfo=UTC),
                end_at=datetime(2026, 7, 2, tzinfo=UTC),
            )
        )


def test_calendar_window_and_factory_configuration_are_bounded() -> None:
    service = MagicMock()
    provider = GoogleCalendarProvider(service, ApprovalService("f" * 32))
    with pytest.raises(ValueError, match="366 天"):
        asyncio.run(
            provider.list_events(
                start_at=datetime(2026, 1, 1, tzinfo=UTC),
                end_at=datetime(2027, 2, 1, tzinfo=UTC),
            )
        )
    service.events.return_value.list.assert_not_called()
    with pytest.raises(ApprovalRequiredError, match="APPROVAL_SIGNING_SECRET"):
        build_approval_service(Settings())
    with pytest.raises(ProviderAuthenticationError, match="GOOGLE_CLIENT_ID"):
        build_google_calendar_provider(Settings(), ApprovalService("g" * 32))
    with pytest.raises(ValidationError, match="APPROVAL_SIGNING_SECRET"):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            service_auth_token="service-token",
        )
