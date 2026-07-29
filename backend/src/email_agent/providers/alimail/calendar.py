"""阿里邮箱开放平台日历 Provider。"""

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ...calendar import ApprovalAction, ApprovalService
from ...contracts import (
    CalendarEvent,
    CalendarEventInput,
    CalendarRecurrence,
    ProviderCapabilities,
    ProviderName,
    ProviderUnavailableError,
    RecurrenceFrequency,
    RecurrenceWeekday,
)
from .client import AliMailClient

MAX_CALENDAR_WINDOW = timedelta(days=366)


class AliMailCalendarProvider:
    """使用阿里邮箱开放平台并在写入前消费审批凭证。"""

    def __init__(
        self,
        client: AliMailClient,
        *,
        email: str,
        approvals: ApprovalService,
    ) -> None:
        self._client = client
        self._email = email
        self._approvals = approvals
        self._user_path = f"/v2/users/{quote(email, safe='')}"
        self._calendar_id: str | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        """返回阿里邮箱日历能力。"""
        return ProviderCapabilities(
            provider=ProviderName.ALIMAIL,
            attachments=False,
            contacts=False,
            calendar=True,
            unsubscribe_headers=False,
        )

    async def list_events(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> Sequence[CalendarEvent]:
        """读取默认日历时间窗口并跟随光标分页。"""
        _validate_window(start_at, end_at)
        calendar_id = await self._get_calendar_id()
        cursor = ""
        events: list[CalendarEvent] = []
        while True:
            response = await self._client.request(
                "GET",
                f"{self._user_path}/calendars/{quote(calendar_id, safe='')}/eventsview",
                params={
                    "startTime": start_at.astimezone(UTC).isoformat(),
                    "endTime": end_at.astimezone(UTC).isoformat(),
                    "cursor": cursor,
                },
            )
            events.extend(
                _parse_event(item)
                for item in response.get("events", [])
                if isinstance(item, Mapping) and not item.get("isCancelled")
            )
            cursor = str(response.get("nextCursor") or "")
            if not response.get("hasMore") or not cursor:
                return events

    async def create_event(
        self,
        event: CalendarEventInput,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> CalendarEvent:
        """审批通过后创建阿里邮箱日历事件。"""
        approval_payload = event.model_dump(mode="json")
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.CREATE,
            target_id=None,
            payload=approval_payload,
            idempotency_key=idempotency_key,
        )
        calendar_id = await self._get_calendar_id()
        body = _event_payload(event, organizer=self._email)
        body["extensions"] = {
            "emailAgentIdempotencyKey": hashlib.sha256(idempotency_key.encode()).hexdigest()
        }
        response = await self._client.request(
            "POST",
            f"{self._user_path}/calendars/{quote(calendar_id, safe='')}/events",
            json={"event": body, "notify": bool(event.attendees)},
            retry_read=False,
        )
        event_id = _required_string(response, "eventId")
        return await self._get_event(calendar_id, event_id)

    async def update_event(
        self,
        event_id: str,
        event: CalendarEventInput,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> CalendarEvent:
        """审批内容匹配后更新阿里邮箱日历事件。"""
        event_id = _validated_id(event_id)
        approval_payload = event.model_dump(mode="json")
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.UPDATE,
            target_id=event_id,
            payload=approval_payload,
            idempotency_key=idempotency_key,
        )
        calendar_id = await self._get_calendar_id()
        await self._client.request(
            "POST",
            f"{self._user_path}/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(event_id, safe='')}",
            json={
                "event": _event_payload(event, organizer=self._email),
                "notify": bool(event.attendees),
            },
            retry_read=False,
        )
        return await self._get_event(calendar_id, event_id)

    async def delete_event(
        self,
        event_id: str,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> None:
        """审批目标匹配后删除阿里邮箱日历事件。"""
        event_id = _validated_id(event_id)
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.DELETE,
            target_id=event_id,
            payload={},
            idempotency_key=idempotency_key,
        )
        calendar_id = await self._get_calendar_id()
        await self._client.request(
            "DELETE",
            f"{self._user_path}/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(event_id, safe='')}",
            retry_read=False,
        )

    async def _get_calendar_id(self) -> str:
        if self._calendar_id is not None:
            return self._calendar_id
        response = await self._client.request("GET", f"{self._user_path}/calendars")
        calendars = [item for item in response.get("calendars", []) if isinstance(item, Mapping)]
        selected = next((item for item in calendars if item.get("isSelected")), None)
        calendar = selected or (calendars[0] if calendars else None)
        if calendar is None:
            raise ProviderUnavailableError("阿里邮箱账号没有可用日历")
        self._calendar_id = _required_string(calendar, "id")
        return self._calendar_id

    async def _get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        response = await self._client.request(
            "GET",
            f"{self._user_path}/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(event_id, safe='')}",
        )
        event = response.get("event")
        if not isinstance(event, Mapping):
            raise ProviderUnavailableError("阿里邮箱日历响应缺少 event")
        return _parse_event(event)


def _event_payload(event: CalendarEventInput, *, organizer: str) -> dict[str, Any]:
    timezone = ZoneInfo(event.timezone)
    payload: dict[str, Any] = {
        "subject": event.title,
        "organizer": {"email": organizer},
        "start": _date_time_payload(event.start_at, timezone, event.timezone),
        "end": _date_time_payload(event.end_at, timezone, event.timezone),
        "attendees": [
            {"user": {"email": address}, "type": "required"} for address in event.attendees
        ],
    }
    if event.location is not None:
        payload["locations"] = [{"type": "defaultLocation", "name": event.location}]
    if event.description is not None:
        payload["body"] = {"bodyText": event.description}
    if event.recurrence is not None:
        payload["recurrence"] = _recurrence_payload(event.recurrence, event)
    return payload


def _date_time_payload(value: datetime, timezone: ZoneInfo, name: str) -> dict[str, Any]:
    local = value.astimezone(timezone)
    return {
        "dateTime": local.replace(tzinfo=None).isoformat(timespec="seconds"),
        "timezone": name,
        "isUtc": False,
    }


def _recurrence_payload(
    recurrence: CalendarRecurrence,
    event: CalendarEventInput,
) -> dict[str, Any]:
    local_start = event.start_at.astimezone(ZoneInfo(event.timezone))
    pattern: dict[str, Any] = {
        "type": {
            RecurrenceFrequency.DAILY: "daily",
            RecurrenceFrequency.WEEKLY: "weekly",
            RecurrenceFrequency.MONTHLY: "absoluteMonthly",
            RecurrenceFrequency.YEARLY: "absoluteYearly",
        }[recurrence.frequency],
        "interval": recurrence.interval,
    }
    if recurrence.frequency is RecurrenceFrequency.WEEKLY:
        pattern["daysOfWeek"] = [day.value for day in recurrence.weekdays] or [
            local_start.strftime("%A").casefold()
        ]
    elif recurrence.frequency is RecurrenceFrequency.MONTHLY:
        pattern["dayOfMonth"] = local_start.day
    elif recurrence.frequency is RecurrenceFrequency.YEARLY:
        pattern["month"] = local_start.month
        pattern["dayOfMonth"] = local_start.day

    range_payload: dict[str, Any]
    if recurrence.count is not None:
        range_payload = {
            "type": "numbered",
            "numberOfOccurrences": recurrence.count,
        }
    elif recurrence.until is not None:
        range_payload = {"type": "dateEnd", "endDate": recurrence.until.isoformat()}
    else:
        range_payload = {"type": "noEnd"}
    return {"pattern": pattern, "range": range_payload}


def _parse_event(raw: Mapping[str, Any]) -> CalendarEvent:
    timezone = _event_timezone(raw)
    locations = raw.get("locations") or []
    attendees = raw.get("attendees") or []
    body = raw.get("body") or {}
    return CalendarEvent(
        id=_required_string(raw, "id"),
        title=str(raw.get("subject") or "无标题事件"),
        start_at=_parse_event_time(raw, "start", "startUtc", timezone),
        end_at=_parse_event_time(raw, "end", "endUtc", timezone),
        timezone=timezone,
        attendees=tuple(
            email
            for item in attendees
            if isinstance(item, Mapping)
            for email in [_nested_email(item.get("user"))]
            if email
        ),
        location=(
            _optional_string(locations[0], "name")
            if locations and isinstance(locations[0], Mapping)
            else None
        ),
        description=(_optional_string(body, "bodyText") if isinstance(body, Mapping) else None),
        recurrence=_parse_recurrence(raw.get("recurrence")),
    )


def _event_timezone(raw: Mapping[str, Any]) -> str:
    start = raw.get("start")
    if isinstance(start, Mapping):
        timezone = str(start.get("timezone") or "").strip()
        if timezone:
            return timezone
    return "UTC"


def _parse_event_time(
    raw: Mapping[str, Any],
    local_field: str,
    utc_field: str,
    timezone: str,
) -> datetime:
    utc_value = raw.get(utc_field)
    if isinstance(utc_value, Mapping):
        value = str(utc_value.get("time") or "").strip()
        if value:
            return _parse_aware_datetime(value)
        date_value = str(utc_value.get("date") or "").strip()
        if date_value:
            return datetime.combine(date.fromisoformat(date_value), datetime.min.time(), UTC)
    local_value = raw.get(local_field)
    if isinstance(local_value, Mapping):
        value = str(local_value.get("dateTime") or "").strip()
        if value:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
            return parsed
    raise ProviderUnavailableError(f"阿里邮箱日历响应缺少时间字段：{local_field}")


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderUnavailableError("阿里邮箱返回了无效日历时间") from exc
    if parsed.tzinfo is None:
        raise ProviderUnavailableError("阿里邮箱日历时间缺少时区")
    return parsed


def _parse_recurrence(value: Any) -> CalendarRecurrence | None:
    if not isinstance(value, Mapping):
        return None
    pattern = value.get("pattern")
    range_payload = value.get("range")
    if not isinstance(pattern, Mapping) or not isinstance(range_payload, Mapping):
        return None
    frequency = {
        "daily": RecurrenceFrequency.DAILY,
        "weekly": RecurrenceFrequency.WEEKLY,
        "absoluteMonthly": RecurrenceFrequency.MONTHLY,
        "absoluteYearly": RecurrenceFrequency.YEARLY,
    }.get(str(pattern.get("type") or ""))
    if frequency is None:
        return None
    count = (
        int(range_payload["numberOfOccurrences"])
        if range_payload.get("type") == "numbered"
        and range_payload.get("numberOfOccurrences") is not None
        else None
    )
    until = (
        date.fromisoformat(str(range_payload["endDate"]))
        if range_payload.get("type") == "dateEnd" and range_payload.get("endDate")
        else None
    )
    weekdays = tuple(
        RecurrenceWeekday(str(day))
        for day in pattern.get("daysOfWeek", [])
        if str(day) in {item.value for item in RecurrenceWeekday}
    )
    return CalendarRecurrence(
        frequency=frequency,
        interval=int(pattern.get("interval") or 1),
        count=count,
        until=until,
        weekdays=weekdays,
    )


def _nested_email(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return str(value.get("email") or "").strip()


def _optional_string(payload: Mapping[str, Any], field: str) -> str | None:
    value = str(payload.get(field) or "").strip()
    return value or None


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = _optional_string(payload, field)
    if value is None:
        raise ProviderUnavailableError(f"阿里邮箱响应缺少字段：{field}")
    return value


def _validated_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("日历事件 ID 不能为空")
    return value


def _validate_window(start_at: datetime, end_at: datetime) -> None:
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise ValueError("日历查询时间必须包含时区")
    if end_at <= start_at:
        raise ValueError("日历查询结束时间必须晚于开始时间")
    if end_at - start_at > MAX_CALENDAR_WINDOW:
        raise ValueError("日历查询窗口不能超过 366 天")
