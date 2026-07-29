"""邮件与日历 Provider 共用的领域契约。"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    """禁止额外字段并保持不可变的基础契约模型。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ProviderName(StrEnum):
    """支持的外部服务。"""

    GMAIL = "gmail"
    OUTLOOK = "outlook"
    ALIMAIL = "alimail"


class ProviderCapabilities(ContractModel):
    """描述 Provider 的实际能力，避免伪造功能对等。"""

    provider: ProviderName
    attachments: bool = True
    contacts: bool = True
    calendar: bool = True
    unsubscribe_headers: bool = False


class MailboxIdentity(ContractModel):
    """当前邮箱身份。"""

    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)


class EmailSummary(ContractModel):
    """邮件列表使用的最小摘要。"""

    id: str = Field(min_length=1, max_length=512)
    thread_id: str | None = Field(default=None, max_length=512)
    subject: str = Field(default="", max_length=998)
    sender: str = Field(min_length=1, max_length=320)
    recipients: tuple[str, ...] = ()
    sent_at: datetime
    snippet: str = Field(default="", max_length=2000)
    is_read: bool = False
    has_attachments: bool = False


class EmailMessage(EmailSummary):
    """包含正文和标准化头信息的完整邮件。"""

    body_text: str = ""
    body_html: str | None = None
    headers: Mapping[str, str] = Field(default_factory=dict)


class EmailSearchFolder(StrEnum):
    """Provider 无关的邮件搜索范围。"""

    ANY = "any"
    INBOX = "inbox"


class EmailSearchCriteria(ContractModel):
    """由各 Provider 翻译执行的统一邮件搜索条件。"""

    folder: EmailSearchFolder = EmailSearchFolder.ANY
    since: datetime | None = None
    query: str | None = Field(default=None, min_length=1, max_length=500)
    keywords: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("since")
    @classmethod
    def validate_since(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("邮件搜索起始时间必须包含时区")
        return value

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value or len(value) > 100 for value in normalized):
            raise ValueError("邮件搜索关键词长度无效")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("邮件搜索关键词不能重复")
        return normalized


class Attachment(ContractModel):
    """邮件附件元数据。"""

    id: str = Field(min_length=1, max_length=512)
    email_id: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)


class Contact(ContractModel):
    """邮件联系人。"""

    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)


class SendEmailRequest(ContractModel):
    """发送或回复邮件所需的确定性字段。"""

    to: tuple[str, ...] = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=1_000_000)
    cc: tuple[str, ...] = Field(default=(), max_length=100)
    bcc: tuple[str, ...] = Field(default=(), max_length=100)
    reply_to_email_id: str | None = Field(default=None, max_length=512)


class RecurrenceFrequency(StrEnum):
    """首期支持的日历重复频率。"""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class RecurrenceWeekday(StrEnum):
    """跨 Google 与 Microsoft 的统一星期枚举。"""

    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


class CalendarRecurrence(ContractModel):
    """受限且可映射到两个日历供应商的重复规则。"""

    frequency: RecurrenceFrequency
    interval: int = Field(default=1, ge=1, le=99)
    count: int | None = Field(default=None, ge=1, le=999)
    until: date | None = None
    weekdays: tuple[RecurrenceWeekday, ...] = Field(default=(), max_length=7)

    @model_validator(mode="after")
    def validate_recurrence(self) -> "CalendarRecurrence":
        """限制结束条件和仅适用于每周重复的星期参数。"""
        if self.count is not None and self.until is not None:
            raise ValueError("重复规则的 count 和 until 不能同时设置")
        if self.weekdays and self.frequency is not RecurrenceFrequency.WEEKLY:
            raise ValueError("只有 weekly 重复规则可以设置 weekdays")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("重复规则的 weekdays 不能重复")
        return self


class CalendarEventInput(ContractModel):
    """创建或修改日历事件的输入。"""

    title: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime
    timezone: str = Field(min_length=1, max_length=100)
    attendees: tuple[str, ...] = Field(default=(), max_length=500)
    location: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=20_000)
    recurrence: CalendarRecurrence | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """要求使用可由服务端解析的 IANA 时区。"""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone 必须是有效的 IANA 时区") from exc
        return value

    @field_validator("attendees")
    @classmethod
    def validate_attendees(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """拒绝重复或明显无效的参与者邮箱。"""
        normalized = tuple(value.casefold() for value in values)
        if len(set(normalized)) != len(values):
            raise ValueError("attendees 不能包含重复邮箱")
        for value in values:
            local, separator, domain = value.partition("@")
            if (
                not separator
                or not local
                or not domain
                or "@" in domain
                or len(local) > 64
                or len(domain) > 255
                or any(character.isspace() for character in value)
            ):
                raise ValueError("attendees 必须是有效邮箱地址")
        return values

    @model_validator(mode="after")
    def validate_time_range(self) -> "CalendarEventInput":
        """要求时间带时区，且结束时间严格晚于开始时间。"""
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("日历事件时间必须包含时区")
        if self.end_at <= self.start_at:
            raise ValueError("日历事件结束时间必须晚于开始时间")
        if self.recurrence and self.recurrence.until:
            local_start = self.start_at.astimezone(ZoneInfo(self.timezone)).date()
            if self.recurrence.until < local_start:
                raise ValueError("重复规则结束日期不能早于事件开始日期")
        return self


class CalendarEvent(CalendarEventInput):
    """已经由 Provider 保存的日历事件。"""

    id: str = Field(min_length=1, max_length=512)


class ProviderError(RuntimeError):
    """可安全映射为统一服务错误的 Provider 基础异常。"""

    code = "provider_error"
    retryable = False


class ProviderAuthenticationError(ProviderError):
    """Provider 认证失效。"""

    code = "provider_authentication_error"


class ProviderPermissionError(ProviderError):
    """Provider 权限不足。"""

    code = "provider_permission_error"


class ProviderRateLimitError(ProviderError):
    """Provider 触发限流。"""

    code = "provider_rate_limit_error"
    retryable = True


class ProviderTimeoutError(ProviderError):
    """Provider 请求超时。"""

    code = "provider_timeout_error"
    retryable = True


class ProviderNotFoundError(ProviderError):
    """Provider 资源不存在。"""

    code = "provider_not_found_error"


class ProviderUnavailableError(ProviderError):
    """Provider 暂时不可用。"""

    code = "provider_unavailable_error"
    retryable = True


class UnsupportedCapabilityError(ProviderError):
    """当前 Provider 不支持所请求能力。"""

    code = "unsupported_capability"


@runtime_checkable
class MailProvider(Protocol):
    """所有邮件 Provider 必须共同实现的异步接口。"""

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def get_identity(self) -> MailboxIdentity: ...

    async def read_inbox(
        self,
        *,
        limit: int,
        unread_only: bool = False,
    ) -> Sequence[EmailSummary]: ...

    async def search_emails(
        self,
        *,
        criteria: EmailSearchCriteria,
        limit: int,
    ) -> Sequence[EmailSummary]: ...

    async def get_email(self, email_id: str) -> EmailMessage: ...

    async def get_sent_emails(self, *, limit: int) -> Sequence[EmailSummary]: ...

    async def get_unanswered_emails(
        self,
        *,
        limit: int,
        since: datetime | None = None,
    ) -> Sequence[EmailSummary]: ...

    async def list_attachments(self, email_id: str) -> Sequence[Attachment]: ...

    async def download_attachment(self, email_id: str, attachment_id: str) -> bytes: ...

    async def list_contacts(self, *, limit: int) -> Sequence[Contact]: ...

    async def send_email(self, request: SendEmailRequest, *, idempotency_key: str) -> str: ...

    async def mark_read(self, email_id: str, *, idempotency_key: str) -> None: ...


@runtime_checkable
class CalendarProvider(Protocol):
    """所有日历 Provider 必须共同实现的异步接口。"""

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def list_events(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> Sequence[CalendarEvent]: ...

    async def create_event(
        self,
        event: CalendarEventInput,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> CalendarEvent: ...

    async def update_event(
        self,
        event_id: str,
        event: CalendarEventInput,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> CalendarEvent: ...

    async def delete_event(
        self,
        event_id: str,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> None: ...
