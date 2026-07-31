"""用户隔离的联系人画像、待回复状态和发信后同步。"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from email.utils import parseaddr
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import AuthContext
from .contracts import EmailSummary, MailProvider
from .persistence import ApplicationState


class CrmContactType(StrEnum):
    PERSON = "person"
    SERVICE = "service"
    NOTIFICATION = "notification"


class CrmPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CrmContact(BaseModel):
    """从邮箱事实和 Agent 审批画像组成的联系人记录。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    frequency: int = Field(default=0, ge=0)
    last_contact: datetime | None = None
    contact_type: CrmContactType
    company: str | None = Field(default=None, max_length=200)
    relationship: str | None = Field(default=None, max_length=200)
    priority: CrmPriority
    deal: str | None = Field(default=None, max_length=500)
    next_contact_date: date | None = None
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    notes: str | None = Field(default=None, max_length=2000)
    needs_reply: bool = False
    updated_at: datetime

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value or len(value) > 50 for value in normalized):
            raise ValueError("CRM 标签长度无效")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("CRM 标签不能重复")
        return normalized


class CrmInitializationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contacts_saved: int
    needs_reply: int
    top_contacts: tuple[CrmContact, ...]


class CrmService:
    """从 Provider 聚合 CRM 事实，并保存到可信用户命名空间。"""

    def __init__(
        self,
        provider: MailProvider,
        state: ApplicationState,
        auth: AuthContext,
    ) -> None:
        self._provider = provider
        self._state = state
        self._user_id = auth.user_id

    async def initialize(
        self,
        *,
        max_emails: int = 200,
        top_n: int = 20,
        exclude_domains: tuple[str, ...] = (),
    ) -> CrmInitializationResult:
        if not 1 <= max_emails <= 500:
            raise ValueError("max_emails 必须在 1 到 500 之间")
        if not 1 <= top_n <= 100:
            raise ValueError("top_n 必须在 1 到 100 之间")
        normalized_exclusions = {
            domain.strip().casefold().lstrip("@")
            for domain in exclude_domains
            if domain.strip()
        }
        identity, address_book, inbox, sent, unanswered = await asyncio.gather(
            self._provider.get_identity(),
            self._provider.list_contacts(limit=min(max_emails, 100)),
            self._provider.read_inbox(limit=max_emails),
            self._provider.get_sent_emails(limit=max_emails),
            self._provider.get_unanswered_emails(limit=min(max_emails, 100), since=None),
        )
        facts: dict[str, dict[str, object]] = {}
        for contact in address_book:
            email = _normalize_email(contact.email)
            if _included(email, identity.email, normalized_exclusions):
                facts[email] = {
                    "display_name": contact.display_name,
                    "frequency": 0,
                    "last_contact": None,
                    "needs_reply": False,
                }
        for message in inbox:
            _merge_message(facts, message.sender, message, needs_reply=False)
        for message in sent:
            for recipient in message.recipients:
                _merge_message(facts, recipient, message, needs_reply=False)
        for message in unanswered:
            _merge_message(facts, message.sender, message, needs_reply=True)

        candidates = [
            (email, fact)
            for email, fact in facts.items()
            if _included(email, identity.email, normalized_exclusions)
        ]
        candidates.sort(
            key=lambda item: (
                not bool(item[1]["needs_reply"]),
                -int(item[1]["frequency"]),
                item[0],
            )
        )
        saved: list[CrmContact] = []
        for email, fact in candidates[:top_n]:
            existing = await asyncio.to_thread(
                self._state.get_crm_contact,
                self._user_id,
                email,
            )
            record = _build_contact(email, fact, existing)
            await asyncio.to_thread(
                self._state.put_crm_contact,
                self._user_id,
                email,
                record.model_dump(mode="json"),
            )
            saved.append(record)
        return CrmInitializationResult(
            contacts_saved=len(saved),
            needs_reply=sum(record.needs_reply for record in saved),
            top_contacts=tuple(saved[:10]),
        )

    async def get(self, email: str) -> CrmContact | None:
        normalized = _normalize_email(email)
        raw = await asyncio.to_thread(
            self._state.get_crm_contact,
            self._user_id,
            normalized,
        )
        return CrmContact.model_validate(raw) if raw is not None else None

    async def list(self, *, limit: int = 50, needs_reply: bool = False) -> tuple[CrmContact, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        raw_items = await asyncio.to_thread(
            self._state.list_crm_contacts,
            self._user_id,
            100,
        )
        contacts = tuple(CrmContact.model_validate(item) for item in raw_items)
        if needs_reply:
            contacts = tuple(item for item in contacts if item.needs_reply)
        return contacts[:limit]

    async def update_profile(
        self,
        email: str,
        *,
        contact_type: CrmContactType | None = None,
        company: str | None = None,
        relationship: str | None = None,
        priority: CrmPriority | None = None,
        deal: str | None = None,
        next_contact_date: date | None = None,
        tags: tuple[str, ...] | None = None,
        notes: str | None = None,
    ) -> CrmContact:
        normalized = _normalize_email(email)
        existing = await self.get(normalized)
        if existing is None:
            raise ValueError("CRM 联系人不存在，请先初始化")
        changes = {
            key: value
            for key, value in {
                "contact_type": contact_type,
                "company": company,
                "relationship": relationship,
                "priority": priority,
                "deal": deal,
                "next_contact_date": next_contact_date,
                "tags": tags,
                "notes": notes,
            }.items()
            if value is not None
        }
        if not changes:
            raise ValueError("至少提供一个联系人画像更新字段")
        updated = existing.model_copy(update={**changes, "updated_at": datetime.now(UTC)})
        await asyncio.to_thread(
            self._state.put_crm_contact,
            self._user_id,
            normalized,
            updated.model_dump(mode="json"),
        )
        return updated

    async def sync_after_send(self, recipients: tuple[str, ...]) -> None:
        now = datetime.now(UTC)
        for raw_email in recipients:
            email = _normalize_email(raw_email)
            existing = await self.get(email)
            if existing is None:
                kind, priority = _classify(email, needs_reply=False)
                existing = CrmContact(
                    email=email,
                    frequency=0,
                    last_contact=None,
                    contact_type=kind,
                    company=_company(email),
                    priority=priority,
                    updated_at=now,
                )
            updated = existing.model_copy(
                update={
                    "frequency": existing.frequency + 1,
                    "last_contact": now,
                    "next_contact_date": None,
                    "needs_reply": False,
                    "updated_at": now,
                }
            )
            await asyncio.to_thread(
                self._state.put_crm_contact,
                self._user_id,
                email,
                updated.model_dump(mode="json"),
            )


def _merge_message(
    facts: dict[str, dict[str, object]],
    raw_email: str,
    message: EmailSummary,
    *,
    needs_reply: bool,
) -> None:
    email = _normalize_email(raw_email)
    fact = facts.setdefault(
        email,
        {
            "display_name": _display_name(raw_email),
            "frequency": 0,
            "last_contact": None,
            "needs_reply": False,
        },
    )
    fact["frequency"] = int(fact["frequency"]) + 1
    previous = fact["last_contact"]
    if not isinstance(previous, datetime) or message.sent_at > previous:
        fact["last_contact"] = message.sent_at
    fact["needs_reply"] = bool(fact["needs_reply"]) or needs_reply


def _build_contact(
    email: str,
    fact: dict[str, object],
    existing: object,
) -> CrmContact:
    current = CrmContact.model_validate(existing) if existing is not None else None
    needs_reply = bool(fact["needs_reply"])
    kind, priority = _classify(email, needs_reply=needs_reply)
    return CrmContact(
        email=email,
        display_name=str(fact["display_name"]) if fact["display_name"] else None,
        frequency=int(fact["frequency"]),
        last_contact=fact["last_contact"] if isinstance(fact["last_contact"], datetime) else None,
        contact_type=current.contact_type if current else kind,
        company=current.company if current else _company(email),
        relationship=current.relationship if current else None,
        priority=current.priority if current else priority,
        deal=current.deal if current else None,
        next_contact_date=current.next_contact_date if current else None,
        tags=current.tags if current else (),
        notes=current.notes if current else None,
        needs_reply=needs_reply,
        updated_at=datetime.now(UTC),
    )


def _normalize_email(value: str) -> str:
    _, address = parseaddr(value.strip())
    normalized = (address or value).strip().casefold()
    local, separator, domain = normalized.rpartition("@")
    if (
        not separator
        or not local
        or not domain
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("联系人邮箱格式无效")
    return normalized


def _display_name(value: str) -> str | None:
    name, _ = parseaddr(value)
    return name.strip() or None


def _included(email: str, own_email: str, excluded_domains: set[str]) -> bool:
    return email != _normalize_email(own_email) and email.rpartition("@")[2] not in excluded_domains


def _classify(email: str, *, needs_reply: bool) -> tuple[CrmContactType, CrmPriority]:
    local = email.partition("@")[0]
    if any(marker in local for marker in ("noreply", "no-reply", "notify", "notification")):
        return CrmContactType.NOTIFICATION, (
            CrmPriority.HIGH if needs_reply else CrmPriority.LOW
        )
    if local in {"support", "help", "team", "info", "service"}:
        return CrmContactType.SERVICE, CrmPriority.HIGH if needs_reply else CrmPriority.MEDIUM
    return CrmContactType.PERSON, CrmPriority.HIGH if needs_reply else CrmPriority.MEDIUM


def _company(email: str) -> str | None:
    domain = email.rpartition("@")[2]
    generic = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "qq.com", "163.com"}
    return None if domain in generic else domain
