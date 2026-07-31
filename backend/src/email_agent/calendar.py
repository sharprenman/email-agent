"""双日历 Provider 与不可绕过的一次性审批凭证。"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import Http

from .config import AuthContext, Settings
from .contracts import (
    CalendarEvent,
    CalendarEventInput,
    CalendarRecurrence,
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderName,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RecurrenceFrequency,
    RecurrenceWeekday,
)
from .outlook import OutlookProvider, build_outlook_provider
from .persistence import ApplicationState

GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
MAX_CALENDAR_WINDOW = timedelta(days=366)
DEFAULT_APPROVAL_TTL_SECONDS = 300
MAX_APPROVAL_TTL_SECONDS = 900


class ApprovalAction(StrEnum):
    """需要用户明确批准的外部副作用操作。"""

    CREATE = "calendar.create"
    UPDATE = "calendar.update"
    DELETE = "calendar.delete"
    SEND_EMAIL = "mail.send"
    UNSUBSCRIBE_ONE_CLICK = "unsubscribe.one_click"
    UNSUBSCRIBE_MAILTO = "unsubscribe.mailto"


class ApprovalError(RuntimeError):
    """审批凭证错误基类。"""

    code = "approval_error"


class ApprovalRequiredError(ApprovalError):
    """未提供有效审批凭证。"""

    code = "approval_required"


class ApprovalExpiredError(ApprovalError):
    """审批凭证已经过期。"""

    code = "approval_expired"


class ApprovalMismatchError(ApprovalError):
    """审批内容与待执行操作不一致。"""

    code = "approval_mismatch"


class ApprovalConsumedError(ApprovalError):
    """审批凭证已经使用。"""

    code = "approval_consumed"


class ApprovalService:
    """签发并原子消费与具体变更绑定的一次性审批凭证。"""

    def __init__(
        self,
        signing_secret: str,
        *,
        clock: Callable[[], datetime] | None = None,
        state: ApplicationState | None = None,
    ) -> None:
        if len(signing_secret.encode()) < 32:
            raise ValueError("审批签名密钥至少需要 32 字节")
        self._secret = signing_secret.encode()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state = state
        self._consumed: dict[str, int] = {}
        self._consumed_requests: dict[str, int] = {}
        self._lock = threading.Lock()

    def mint_after_interrupt(
        self,
        auth: AuthContext,
        *,
        action: ApprovalAction,
        target_id: str | None,
        payload: Mapping[str, Any],
        idempotency_key: str,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> str:
        """在 DeepAgents interrupt 获得用户批准后签发短期凭证。"""
        _validate_idempotency_key(idempotency_key)
        if not 1 <= ttl_seconds <= MAX_APPROVAL_TTL_SECONDS:
            raise ValueError(f"审批有效期必须在 1 到 {MAX_APPROVAL_TTL_SECONDS} 秒之间")
        now = self._now_timestamp()
        claims = {
            "v": 1,
            "jti": secrets.token_urlsafe(18),
            "sub": auth.user_id,
            "action": action.value,
            "target": target_id,
            "request_hash": _request_hash(action, target_id, payload, idempotency_key),
            "exp": now + ttl_seconds,
        }
        encoded = _base64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        signature = _base64url(hmac.digest(self._secret, encoded.encode(), "sha256"))
        return f"{encoded}.{signature}"

    def consume(
        self,
        token: str,
        *,
        user_id: str,
        action: ApprovalAction,
        target_id: str | None,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> None:
        """校验内容并原子标记凭证已使用，任何不匹配都默认拒绝。"""
        _validate_idempotency_key(idempotency_key)
        claims = self._decode(token)
        now = self._now_timestamp()
        if int(claims.get("exp") or 0) <= now:
            raise ApprovalExpiredError("审批凭证已过期，请重新确认操作")
        expected_hash = _request_hash(action, target_id, payload, idempotency_key)
        if (
            claims.get("sub") != user_id
            or claims.get("action") != action.value
            or claims.get("target") != target_id
            or not hmac.compare_digest(str(claims.get("request_hash") or ""), expected_hash)
        ):
            raise ApprovalMismatchError("审批凭证与当前待执行操作不匹配")
        jti = str(claims.get("jti") or "")
        if not jti:
            raise ApprovalRequiredError("审批凭证缺少唯一标识")
        request_hash = str(claims["request_hash"])
        expires_at = int(claims["exp"])
        if self._state is not None:
            if not self._state.consume_approval(jti, user_id, request_hash, expires_at):
                raise ApprovalConsumedError("审批凭证或操作幂等键已经使用，不能重复恢复")
            return
        with self._lock:
            self._consumed = {
                used_jti: expires_at
                for used_jti, expires_at in self._consumed.items()
                if expires_at >= now
            }
            self._consumed_requests = {
                used_hash: expires_at
                for used_hash, expires_at in self._consumed_requests.items()
                if expires_at >= now
            }
            if jti in self._consumed or request_hash in self._consumed_requests:
                raise ApprovalConsumedError("审批凭证或操作幂等键已经使用，不能重复恢复")
            self._consumed[jti] = expires_at
            self._consumed_requests[request_hash] = expires_at

    def _decode(self, token: str) -> Mapping[str, Any]:
        try:
            encoded, signature = token.split(".", 1)
            expected = _base64url(hmac.digest(self._secret, encoded.encode(), "sha256"))
            if not hmac.compare_digest(signature, expected):
                raise ApprovalRequiredError("审批凭证签名无效")
            payload = json.loads(_decode_base64url(encoded))
        except ApprovalError:
            raise
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ApprovalRequiredError("审批凭证格式无效") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ApprovalRequiredError("审批凭证版本无效")
        return payload

    def _now_timestamp(self) -> int:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("审批时钟必须包含时区")
        return int(current.timestamp())


def build_approval_service(
    settings: Settings,
    state: ApplicationState | None = None,
) -> ApprovalService:
    """从配置创建审批服务，缺少签名密钥时默认拒绝启动写能力。"""
    if settings.approval_signing_secret is None:
        raise ApprovalRequiredError("缺少 APPROVAL_SIGNING_SECRET，外部写能力不可用")
    return ApprovalService(
        settings.approval_signing_secret.get_secret_value(),
        state=state,
    )


class GoogleCalendarProvider:
    """使用 Google Calendar API 并在每次写入前消费审批凭证。"""

    def __init__(
        self,
        service: Any,
        approvals: ApprovalService,
        *,
        read_retries: int = 2,
    ) -> None:
        self._service = service
        self._approvals = approvals
        self._read_retries = read_retries
        self._execute_lock = asyncio.Lock()

    @property
    def capabilities(self) -> ProviderCapabilities:
        """返回 Google Calendar 能力。"""
        return ProviderCapabilities(
            provider=ProviderName.GMAIL,
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
        """按开始时间读取时间窗口内的展开事件实例。"""
        _validate_window(start_at, end_at)
        arguments: dict[str, Any] = {
            "calendarId": "primary",
            "timeMin": start_at.isoformat(),
            "timeMax": end_at.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 2500,
        }
        events: list[CalendarEvent] = []
        while True:
            response = await self._execute(
                self._service.events().list(**arguments),
                retries=self._read_retries,
            )
            events.extend(_parse_google_event(item) for item in response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return events
            arguments["pageToken"] = page_token

    async def create_event(
        self,
        event: CalendarEventInput,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> CalendarEvent:
        """审批通过后创建具有稳定 ID 的 Google 日历事件。"""
        payload = event.model_dump(mode="json")
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.CREATE,
            target_id=None,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        body = _google_event_payload(event)
        body["id"] = _google_idempotent_event_id(idempotency_key)
        result = await self._execute(
            self._service.events().insert(
                calendarId="primary",
                body=body,
                sendUpdates="all",
            ),
            retries=0,
        )
        return _parse_google_event(result)

    async def update_event(
        self,
        event_id: str,
        event: CalendarEventInput,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> CalendarEvent:
        """审批内容完全匹配后替换 Google 日历事件。"""
        _validate_event_id(event_id)
        payload = event.model_dump(mode="json")
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.UPDATE,
            target_id=event_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        result = await self._execute(
            self._service.events().update(
                calendarId="primary",
                eventId=event_id,
                body=_google_event_payload(event),
                sendUpdates="all",
            ),
            retries=0,
        )
        return _parse_google_event(result)

    async def delete_event(
        self,
        event_id: str,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> None:
        """审批目标匹配后删除 Google 日历事件。"""
        _validate_event_id(event_id)
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.DELETE,
            target_id=event_id,
            payload={},
            idempotency_key=idempotency_key,
        )
        await self._execute(
            self._service.events().delete(
                calendarId="primary",
                eventId=event_id,
                sendUpdates="all",
            ),
            retries=0,
        )

    async def _execute(self, request: Any, *, retries: int) -> Mapping[str, Any]:
        async with self._execute_lock:
            try:
                return (await asyncio.to_thread(request.execute, num_retries=retries)) or {}
            except HttpError as exc:
                raise _map_google_error(exc) from exc
            except RefreshError as exc:
                raise ProviderAuthenticationError("Google OAuth 凭证刷新失败") from exc
            except TimeoutError as exc:
                raise ProviderTimeoutError("Google Calendar 请求超时") from exc
            except (TransportError, OSError) as exc:
                raise ProviderUnavailableError("Google Calendar 暂时不可用") from exc


def build_google_calendar_provider(
    settings: Settings,
    approvals: ApprovalService,
) -> GoogleCalendarProvider:
    """使用现有 Google OAuth 配置创建日历 Provider。"""
    required = {
        "GOOGLE_CLIENT_ID": settings.google_client_id,
        "GOOGLE_CLIENT_SECRET": settings.google_client_secret,
        "GOOGLE_REFRESH_TOKEN": settings.google_refresh_token,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ProviderAuthenticationError("缺少 Google OAuth 配置：" + ", ".join(missing))
    credentials = Credentials(
        token=(
            settings.google_access_token.get_secret_value()
            if settings.google_access_token is not None
            else None
        ),
        refresh_token=settings.google_refresh_token.get_secret_value(),
        token_uri=GOOGLE_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        scopes=(GOOGLE_CALENDAR_SCOPE,),
    )
    authorized_http = AuthorizedHttp(
        credentials,
        http=Http(timeout=settings.provider_timeout_seconds),
    )
    service = build("calendar", "v3", http=authorized_http, cache_discovery=False)
    return GoogleCalendarProvider(service, approvals)


class GraphRequester(Protocol):
    """Microsoft Calendar 复用的最小 Graph 请求接口。"""

    async def request_graph(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        retry_read: bool = True,
    ) -> Mapping[str, Any]: ...

    async def aclose(self) -> None: ...


class MicrosoftCalendarProvider:
    """使用 Microsoft Graph 并在每次写入前消费审批凭证。"""

    def __init__(self, graph: GraphRequester, approvals: ApprovalService) -> None:
        self._graph = graph
        self._approvals = approvals

    @property
    def capabilities(self) -> ProviderCapabilities:
        """返回 Microsoft Calendar 能力。"""
        return ProviderCapabilities(
            provider=ProviderName.OUTLOOK,
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
        """读取 Microsoft Calendar 时间窗口并跟随完整分页链接。"""
        _validate_window(start_at, end_at)
        params: Mapping[str, str] | None = {
            "startDateTime": start_at.isoformat(),
            "endDateTime": end_at.isoformat(),
            "$top": "100",
            "$orderby": "start/dateTime",
        }
        url: str | None = "/me/calendarView"
        events: list[CalendarEvent] = []
        while url:
            response = await self._graph.request_graph(
                "GET",
                url,
                params=params,
                headers={"Prefer": 'outlook.timezone="UTC"'},
            )
            events.extend(_parse_microsoft_event(item) for item in response.get("value", []))
            url = _optional_string(response, "@odata.nextLink")
            params = None
        return events

    async def create_event(
        self,
        event: CalendarEventInput,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> CalendarEvent:
        """审批通过后创建带 transactionId 的 Microsoft 日历事件。"""
        payload = event.model_dump(mode="json")
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.CREATE,
            target_id=None,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        body = _microsoft_event_payload(event)
        body["transactionId"] = str(
            uuid.UUID(hashlib.sha256(idempotency_key.encode()).hexdigest()[:32])
        )
        result = await self._graph.request_graph(
            "POST",
            "/me/events",
            headers={"Prefer": 'outlook.timezone="UTC"'},
            json=body,
            retry_read=False,
        )
        return _parse_microsoft_event(result)

    async def update_event(
        self,
        event_id: str,
        event: CalendarEventInput,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> CalendarEvent:
        """审批内容匹配后更新 Microsoft 日历事件。"""
        _validate_event_id(event_id)
        payload = event.model_dump(mode="json")
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.UPDATE,
            target_id=event_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        result = await self._graph.request_graph(
            "PATCH",
            f"/me/events/{event_id}",
            headers={"Prefer": 'outlook.timezone="UTC"'},
            json=_microsoft_event_payload(event),
            retry_read=False,
        )
        return _parse_microsoft_event(result)

    async def delete_event(
        self,
        event_id: str,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> None:
        """审批目标匹配后删除 Microsoft 日历事件。"""
        _validate_event_id(event_id)
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=ApprovalAction.DELETE,
            target_id=event_id,
            payload={},
            idempotency_key=idempotency_key,
        )
        await self._graph.request_graph(
            "DELETE",
            f"/me/events/{event_id}",
            retry_read=False,
        )

    async def aclose(self) -> None:
        """关闭 Microsoft Graph 连接池。"""
        await self._graph.aclose()


def build_microsoft_calendar_provider(
    settings: Settings,
    approvals: ApprovalService,
) -> MicrosoftCalendarProvider:
    """复用 Microsoft OAuth 配置创建日历 Provider。"""
    graph: OutlookProvider = build_outlook_provider(settings)
    return MicrosoftCalendarProvider(graph, approvals)


def _google_event_payload(event: CalendarEventInput) -> dict[str, Any]:
    timezone = ZoneInfo(event.timezone)
    payload: dict[str, Any] = {
        "summary": event.title,
        "start": {
            "dateTime": event.start_at.astimezone(timezone).isoformat(),
            "timeZone": event.timezone,
        },
        "end": {
            "dateTime": event.end_at.astimezone(timezone).isoformat(),
            "timeZone": event.timezone,
        },
        "attendees": [{"email": address} for address in event.attendees],
    }
    if event.location is not None:
        payload["location"] = event.location
    if event.description is not None:
        payload["description"] = event.description
    if event.recurrence is not None:
        payload["recurrence"] = [_google_rrule(event.recurrence, event)]
    return payload


def _parse_google_event(raw: Mapping[str, Any]) -> CalendarEvent:
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    timezone = str(start.get("timeZone") or end.get("timeZone") or "UTC")
    return CalendarEvent(
        id=_required_string(raw, "id", "Google Calendar"),
        title=str(raw.get("summary") or "无标题事件"),
        start_at=_parse_google_event_time(start, timezone),
        end_at=_parse_google_event_time(end, timezone),
        timezone=timezone,
        attendees=tuple(
            str(item.get("email") or "")
            for item in raw.get("attendees", []) or []
            if item.get("email")
        ),
        location=_optional_string(raw, "location"),
        description=_optional_string(raw, "description"),
        recurrence=_parse_google_recurrence(raw.get("recurrence") or []),
    )


def _parse_google_event_time(value: Mapping[str, Any], timezone: str) -> datetime:
    if value.get("dateTime"):
        return _parse_datetime(str(value["dateTime"]), timezone)
    if value.get("date"):
        return datetime.combine(
            date.fromisoformat(str(value["date"])), time.min, ZoneInfo(timezone)
        )
    raise ProviderUnavailableError("Google Calendar 响应缺少事件时间")


def _google_rrule(recurrence: CalendarRecurrence, event: CalendarEventInput) -> str:
    fields = [f"FREQ={recurrence.frequency.value.upper()}", f"INTERVAL={recurrence.interval}"]
    if recurrence.count is not None:
        fields.append(f"COUNT={recurrence.count}")
    if recurrence.until is not None:
        fields.append(f"UNTIL={recurrence.until.strftime('%Y%m%d')}T235959Z")
    weekdays = recurrence.weekdays or (
        (
            RecurrenceWeekday(
                event.start_at.astimezone(ZoneInfo(event.timezone)).strftime("%A").lower()
            ),
        )
        if recurrence.frequency is RecurrenceFrequency.WEEKLY
        else ()
    )
    if weekdays:
        fields.append("BYDAY=" + ",".join(_GOOGLE_WEEKDAYS[weekday] for weekday in weekdays))
    return "RRULE:" + ";".join(fields)


def _parse_google_recurrence(values: Sequence[str]) -> CalendarRecurrence | None:
    rrule = next((value for value in values if value.startswith("RRULE:")), None)
    if rrule is None:
        return None
    fields = dict(
        item.split("=", 1) for item in rrule.removeprefix("RRULE:").split(";") if "=" in item
    )
    frequency = RecurrenceFrequency(fields.get("FREQ", "").casefold())
    until = fields.get("UNTIL")
    return CalendarRecurrence(
        frequency=frequency,
        interval=int(fields.get("INTERVAL", "1")),
        count=int(fields["COUNT"]) if fields.get("COUNT") else None,
        until=datetime.strptime(until[:8], "%Y%m%d").date() if until else None,
        weekdays=tuple(
            _GOOGLE_WEEKDAYS_REVERSE[item] for item in fields.get("BYDAY", "").split(",") if item
        ),
    )


def _microsoft_event_payload(event: CalendarEventInput) -> dict[str, Any]:
    timezone = ZoneInfo(event.timezone)
    payload: dict[str, Any] = {
        "subject": event.title,
        "start": {
            "dateTime": event.start_at.astimezone(timezone).replace(tzinfo=None).isoformat(),
            "timeZone": event.timezone,
        },
        "end": {
            "dateTime": event.end_at.astimezone(timezone).replace(tzinfo=None).isoformat(),
            "timeZone": event.timezone,
        },
        "attendees": [
            {"emailAddress": {"address": address}, "type": "required"}
            for address in event.attendees
        ],
    }
    if event.location is not None:
        payload["location"] = {"displayName": event.location}
    if event.description is not None:
        payload["body"] = {"contentType": "text", "content": event.description}
    if event.recurrence is not None:
        payload["recurrence"] = _microsoft_recurrence(event.recurrence, event)
    return payload


def _parse_microsoft_event(raw: Mapping[str, Any]) -> CalendarEvent:
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    timezone = str(start.get("timeZone") or end.get("timeZone") or "UTC")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ProviderUnavailableError("Microsoft Calendar 返回了非 IANA 时区") from exc
    location = raw.get("location") or {}
    body = raw.get("body") or {}
    return CalendarEvent(
        id=_required_string(raw, "id", "Microsoft Calendar"),
        title=str(raw.get("subject") or "无标题事件"),
        start_at=_parse_datetime(
            _required_string(start, "dateTime", "Microsoft Calendar"), timezone
        ),
        end_at=_parse_datetime(_required_string(end, "dateTime", "Microsoft Calendar"), timezone),
        timezone=timezone,
        attendees=tuple(
            address
            for address in (
                str((item.get("emailAddress") or {}).get("address") or "").strip()
                for item in raw.get("attendees", []) or []
            )
            if address
        ),
        location=str(location.get("displayName") or "").strip() or None,
        description=str(body.get("content") or "").strip() or None,
        recurrence=_parse_microsoft_recurrence(raw.get("recurrence")),
    )


def _microsoft_recurrence(
    recurrence: CalendarRecurrence,
    event: CalendarEventInput,
) -> dict[str, Any]:
    local_start = event.start_at.astimezone(ZoneInfo(event.timezone)).date()
    weekdays = recurrence.weekdays or (
        (RecurrenceWeekday(local_start.strftime("%A").lower()),)
        if recurrence.frequency is RecurrenceFrequency.WEEKLY
        else ()
    )
    pattern: dict[str, Any] = {
        "type": recurrence.frequency.value,
        "interval": recurrence.interval,
    }
    if weekdays:
        pattern["daysOfWeek"] = [weekday.value for weekday in weekdays]
    if recurrence.frequency is RecurrenceFrequency.MONTHLY:
        pattern["dayOfMonth"] = local_start.day
        pattern["type"] = "absoluteMonthly"
    elif recurrence.frequency is RecurrenceFrequency.YEARLY:
        pattern.update(
            {"type": "absoluteYearly", "dayOfMonth": local_start.day, "month": local_start.month}
        )
    recurrence_range: dict[str, Any] = {
        "type": "noEnd",
        "startDate": local_start.isoformat(),
        "recurrenceTimeZone": event.timezone,
    }
    if recurrence.count is not None:
        recurrence_range.update({"type": "numbered", "numberOfOccurrences": recurrence.count})
    elif recurrence.until is not None:
        recurrence_range.update({"type": "endDate", "endDate": recurrence.until.isoformat()})
    return {"pattern": pattern, "range": recurrence_range}


def _parse_microsoft_recurrence(value: Any) -> CalendarRecurrence | None:
    if not isinstance(value, Mapping):
        return None
    pattern = value.get("pattern") or {}
    recurrence_range = value.get("range") or {}
    raw_type = str(pattern.get("type") or "").casefold()
    frequency_by_type = {
        "daily": RecurrenceFrequency.DAILY,
        "weekly": RecurrenceFrequency.WEEKLY,
        "absolutemonthly": RecurrenceFrequency.MONTHLY,
        "absoluteyearly": RecurrenceFrequency.YEARLY,
    }
    frequency = frequency_by_type.get(raw_type)
    if frequency is None:
        raise ProviderUnavailableError("Microsoft Calendar 返回了不支持的重复规则")
    return CalendarRecurrence(
        frequency=frequency,
        interval=int(pattern.get("interval") or 1),
        count=(
            int(recurrence_range["numberOfOccurrences"])
            if recurrence_range.get("type") == "numbered"
            else None
        ),
        until=(
            date.fromisoformat(str(recurrence_range["endDate"]))
            if recurrence_range.get("type") == "endDate"
            else None
        ),
        weekdays=tuple(
            RecurrenceWeekday(str(item).casefold()) for item in pattern.get("daysOfWeek", []) or []
        ),
    )


def _request_hash(
    action: ApprovalAction,
    target_id: str | None,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> str:
    canonical = json.dumps(
        {
            "action": action.value,
            "target": target_id,
            "payload": payload,
            "idempotency_key": idempotency_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _google_idempotent_event_id(idempotency_key: str) -> str:
    _validate_idempotency_key(idempotency_key)
    return "ea" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:30]


def _validate_idempotency_key(value: str) -> None:
    if not value.strip():
        raise ValueError("外部写操作必须提供幂等键")


def _validate_event_id(event_id: str) -> None:
    if not event_id.strip():
        raise ValueError("日历事件 ID 不能为空")


def _validate_window(start_at: datetime, end_at: datetime) -> None:
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise ValueError("日历查询时间必须包含时区")
    if end_at <= start_at:
        raise ValueError("日历查询结束时间必须晚于开始时间")
    if end_at - start_at > MAX_CALENDAR_WINDOW:
        raise ValueError("日历查询时间范围不能超过 366 天")


def _parse_datetime(value: str, timezone: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderUnavailableError("日历 Provider 返回了无效时间") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=ZoneInfo(timezone))


def _required_string(payload: Mapping[str, Any], field: str, provider: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ProviderUnavailableError(f"{provider} 响应缺少字段：{field}")
    return value


def _optional_string(payload: Mapping[str, Any], field: str) -> str | None:
    value = str(payload.get(field) or "").strip()
    return value or None


def _map_google_error(error: HttpError) -> Exception:
    status = int(getattr(error.resp, "status", 0) or 0)
    if status == 401:
        return ProviderAuthenticationError("Google Calendar 认证失败")
    if status == 403:
        return ProviderPermissionError("Google Calendar 权限不足")
    if status == 404:
        return ProviderNotFoundError("Google Calendar 事件不存在")
    if status == 429:
        return ProviderRateLimitError("Google Calendar 请求受限")
    if status in {408, 504}:
        return ProviderTimeoutError("Google Calendar 请求超时")
    return ProviderUnavailableError("Google Calendar 请求失败")


_GOOGLE_WEEKDAYS = {
    RecurrenceWeekday.MONDAY: "MO",
    RecurrenceWeekday.TUESDAY: "TU",
    RecurrenceWeekday.WEDNESDAY: "WE",
    RecurrenceWeekday.THURSDAY: "TH",
    RecurrenceWeekday.FRIDAY: "FR",
    RecurrenceWeekday.SATURDAY: "SA",
    RecurrenceWeekday.SUNDAY: "SU",
}
_GOOGLE_WEEKDAYS_REVERSE = {value: key for key, value in _GOOGLE_WEEKDAYS.items()}
