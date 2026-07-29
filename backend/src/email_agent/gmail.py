"""基于 Gmail API 和 People API 的邮件 Provider。"""

import asyncio
import base64
import binascii
import hashlib
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from email.message import EmailMessage as MimeEmailMessage
from email.utils import getaddresses, parseaddr
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import Http

from .config import Settings
from .contracts import (
    Attachment,
    Contact,
    EmailMessage,
    EmailSearchCriteria,
    EmailSearchFolder,
    EmailSummary,
    MailboxIdentity,
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderName,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    SendEmailRequest,
    UnsupportedCapabilityError,
)

GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/contacts.readonly",
)
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailProvider:
    """把 Google API 响应转换为统一邮件领域契约。"""

    def __init__(
        self,
        gmail_service: Any,
        people_service: Any | None = None,
        *,
        read_retries: int = 2,
    ) -> None:
        self._gmail = gmail_service
        self._people = people_service
        self._read_retries = read_retries
        self._execute_lock = asyncio.Lock()

    @property
    def capabilities(self) -> ProviderCapabilities:
        """返回当前已装配的 Gmail 能力。"""
        return ProviderCapabilities(
            provider=ProviderName.GMAIL,
            attachments=True,
            contacts=self._people is not None,
            calendar=False,
            unsubscribe_headers=True,
        )

    async def get_identity(self) -> MailboxIdentity:
        """读取当前 Gmail 主邮箱地址。"""
        result = await self._execute(
            self._gmail.users().getProfile(userId="me"),
            retries=self._read_retries,
        )
        return MailboxIdentity(email=_required_string(result, "emailAddress"))

    async def read_inbox(
        self,
        *,
        limit: int,
        unread_only: bool = False,
    ) -> Sequence[EmailSummary]:
        """读取收件箱摘要。"""
        query = "is:unread" if unread_only else None
        return await self._list_summaries(limit=limit, label_ids=["INBOX"], query=query)

    async def search_emails(
        self,
        *,
        criteria: EmailSearchCriteria,
        limit: int,
    ) -> Sequence[EmailSummary]:
        """把统一搜索条件翻译为 Gmail 查询语法。"""
        return await self._list_summaries(
            limit=limit,
            query=_gmail_search_query(criteria),
        )

    async def get_email(self, email_id: str) -> EmailMessage:
        """读取完整邮件正文和标准化头信息。"""
        raw = await self._get_raw_message(email_id)
        return _parse_message(raw)

    async def get_sent_emails(self, *, limit: int) -> Sequence[EmailSummary]:
        """读取已发送邮件摘要。"""
        return await self._list_summaries(limit=limit, label_ids=["SENT"])

    async def get_unanswered_emails(
        self,
        *,
        limit: int,
        since: datetime | None = None,
    ) -> Sequence[EmailSummary]:
        """返回最后一封消息来自对方的收件箱线程。"""
        _validate_limit(limit)
        identity = (await self.get_identity()).email.casefold()
        query = "in:inbox"
        if since is not None:
            query += f" after:{since.date().isoformat().replace('-', '/')}"
        response = await self._execute(
            self._gmail.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(limit * 3, 100),
                includeSpamTrash=False,
            ),
            retries=self._read_retries,
        )
        thread_ids = list(
            dict.fromkeys(
                item.get("threadId")
                for item in response.get("messages", [])
                if item.get("threadId")
            )
        )
        results: list[EmailSummary] = []
        for thread_id in thread_ids:
            thread = await self._execute(
                self._gmail.users().threads().get(userId="me", id=thread_id, format="full"),
                retries=self._read_retries,
            )
            messages = sorted(thread.get("messages", []), key=_internal_date)
            if not messages:
                continue
            latest = _parse_summary(messages[-1])
            if (
                _address(latest.sender).casefold() != identity
                and (since is None or latest.sent_at >= since)
            ):
                results.append(latest)
            if len(results) == limit:
                break
        return results

    async def list_attachments(self, email_id: str) -> Sequence[Attachment]:
        """列出邮件中所有嵌套附件的元数据。"""
        raw = await self._get_raw_message(email_id)
        return list(_iter_attachments(raw.get("payload", {}), email_id))

    async def download_attachment(self, email_id: str, attachment_id: str) -> bytes:
        """下载指定附件正文并解码 Gmail 的 base64url 表示。"""
        if not email_id.strip() or not attachment_id.strip():
            raise ValueError("邮件 ID 和附件 ID 不能为空")
        result = await self._execute(
            self._gmail.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=email_id, id=attachment_id),
            retries=self._read_retries,
        )
        data = _required_string(result, "data")
        try:
            return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        except (ValueError, binascii.Error) as exc:
            raise ProviderUnavailableError("Gmail 返回了无效附件正文") from exc

    async def list_contacts(self, *, limit: int) -> Sequence[Contact]:
        """通过 People API 读取联系人。"""
        _validate_limit(limit, maximum=1000)
        if self._people is None:
            raise UnsupportedCapabilityError("Gmail Provider 未装配 People API")
        response = await self._execute(
            self._people.people()
            .connections()
            .list(
                resourceName="people/me",
                pageSize=limit,
                personFields="names,emailAddresses",
            ),
            retries=self._read_retries,
        )
        contacts: list[Contact] = []
        for person in response.get("connections", []):
            emails = person.get("emailAddresses") or []
            if not emails:
                continue
            names = person.get("names") or []
            contacts.append(
                Contact(
                    email=_primary_value(emails),
                    display_name=_primary_name(names) if names else None,
                )
            )
        return contacts[:limit]

    async def send_email(self, request: SendEmailRequest, *, idempotency_key: str) -> str:
        """发送新邮件或在线程中回复；持久化幂等由上层服务负责。"""
        if not idempotency_key.strip():
            raise ValueError("发送邮件必须提供幂等键")
        mime = MimeEmailMessage()
        mime.set_content(request.body)
        mime["To"] = ", ".join(request.to)
        mime["Subject"] = request.subject
        if request.cc:
            mime["Cc"] = ", ".join(request.cc)
        if request.bcc:
            mime["Bcc"] = ", ".join(request.bcc)
        mime["X-Email-Agent-Idempotency-Key"] = hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()

        body: dict[str, str] = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}
        if request.reply_to_email_id:
            original = await self._get_raw_message(request.reply_to_email_id)
            headers = _headers(original.get("payload", {}))
            message_id = headers.get("message-id")
            if message_id:
                mime["In-Reply-To"] = message_id
                mime["References"] = message_id
                body["raw"] = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
            thread_id = original.get("threadId")
            if thread_id:
                body["threadId"] = thread_id

        result = await self._execute(
            self._gmail.users().messages().send(userId="me", body=body),
            retries=0,
        )
        return _required_string(result, "id")

    async def mark_read(self, email_id: str, *, idempotency_key: str) -> None:
        """移除 Gmail 的 UNREAD 标签。"""
        if not idempotency_key.strip():
            raise ValueError("修改邮件状态必须提供幂等键")
        await self._execute(
            self._gmail.users()
            .messages()
            .modify(
                userId="me",
                id=email_id,
                body={"removeLabelIds": ["UNREAD"]},
            ),
            retries=0,
        )

    async def _list_summaries(
        self,
        *,
        limit: int,
        label_ids: list[str] | None = None,
        query: str | None = None,
    ) -> Sequence[EmailSummary]:
        _validate_limit(limit)
        list_arguments: dict[str, Any] = {
            "userId": "me",
            "maxResults": limit,
            "includeSpamTrash": False,
        }
        if label_ids is not None:
            list_arguments["labelIds"] = label_ids
        if query is not None:
            list_arguments["q"] = query
        references: list[Mapping[str, Any]] = []
        while len(references) < limit:
            list_arguments["maxResults"] = limit - len(references)
            response = await self._execute(
                self._gmail.users().messages().list(**list_arguments),
                retries=self._read_retries,
            )
            references.extend(response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
            list_arguments["pageToken"] = page_token

        results: list[EmailSummary] = []
        for reference in references[:limit]:
            raw = await self._get_raw_message(_required_string(reference, "id"))
            results.append(_parse_summary(raw))
        return results

    async def _get_raw_message(self, email_id: str) -> Mapping[str, Any]:
        if not email_id.strip():
            raise ValueError("邮件 ID 不能为空")
        return await self._execute(
            self._gmail.users().messages().get(userId="me", id=email_id, format="full"),
            retries=self._read_retries,
        )

    async def _execute(self, request: Any, *, retries: int) -> Mapping[str, Any]:
        async with self._execute_lock:
            try:
                return await asyncio.to_thread(request.execute, num_retries=retries)
            except HttpError as exc:
                raise _map_http_error(exc) from exc
            except RefreshError as exc:
                raise ProviderAuthenticationError("Google OAuth 凭证刷新失败") from exc
            except TimeoutError as exc:
                raise ProviderTimeoutError("Google API 请求超时") from exc
            except (TransportError, OSError) as exc:
                raise ProviderUnavailableError("Google API 暂时不可用") from exc


def build_gmail_provider(settings: Settings) -> GmailProvider:
    """使用环境中的 OAuth 凭证创建 Gmail 和 People 服务。"""
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
        scopes=GMAIL_SCOPES,
    )
    authorized_http = AuthorizedHttp(
        credentials,
        http=Http(timeout=settings.provider_timeout_seconds),
    )
    gmail_service = build("gmail", "v1", http=authorized_http, cache_discovery=False)
    people_service = build("people", "v1", http=authorized_http, cache_discovery=False)
    return GmailProvider(gmail_service, people_service)


def _parse_message(raw: Mapping[str, Any]) -> EmailMessage:
    summary = _parse_summary(raw)
    payload = raw.get("payload", {})
    text, html = _extract_bodies(payload)
    return EmailMessage(
        **summary.model_dump(),
        body_text=text,
        body_html=html,
        headers=_headers(payload),
    )


def _parse_summary(raw: Mapping[str, Any]) -> EmailSummary:
    payload = raw.get("payload", {})
    headers = _headers(payload)
    recipients = tuple(
        address
        for _, address in getaddresses([headers.get("to", ""), headers.get("cc", "")])
        if address
    )
    return EmailSummary(
        id=_required_string(raw, "id"),
        thread_id=raw.get("threadId"),
        subject=headers.get("subject", ""),
        sender=_address(headers.get("from", "unknown")) or "unknown",
        recipients=recipients,
        sent_at=datetime.fromtimestamp(_internal_date(raw) / 1000, tz=UTC),
        snippet=str(raw.get("snippet", "")),
        is_read="UNREAD" not in raw.get("labelIds", []),
        has_attachments=any(_iter_attachments(payload, _required_string(raw, "id"))),
    )


def _extract_bodies(part: Mapping[str, Any]) -> tuple[str, str | None]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    for current in _walk_parts(part):
        data = current.get("body", {}).get("data")
        if not data:
            continue
        decoded = _decode_base64url(data)
        if current.get("mimeType") == "text/plain":
            text_parts.append(decoded)
        elif current.get("mimeType") == "text/html":
            html_parts.append(decoded)
    return "\n".join(text_parts), "\n".join(html_parts) or None


def _iter_attachments(part: Mapping[str, Any], email_id: str) -> Iterator[Attachment]:
    for current in _walk_parts(part):
        body = current.get("body", {})
        attachment_id = body.get("attachmentId")
        filename = current.get("filename")
        if attachment_id and filename:
            yield Attachment(
                id=str(attachment_id),
                email_id=email_id,
                filename=str(filename),
                content_type=str(current.get("mimeType") or "application/octet-stream"),
                size_bytes=int(body.get("size") or 0),
            )


def _walk_parts(part: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


def _headers(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item.get("name", "")).casefold(): str(item.get("value", ""))
        for item in payload.get("headers", []) or []
        if item.get("name")
    }


def _primary_value(items: Sequence[Mapping[str, Any]]) -> str:
    primary = next(
        (item for item in items if item.get("metadata", {}).get("primary")),
        items[0],
    )
    return _required_string(primary, "value")


def _primary_name(items: Sequence[Mapping[str, Any]]) -> str:
    primary = next(
        (item for item in items if item.get("metadata", {}).get("primary")),
        items[0],
    )
    return _required_string(primary, "displayName")


def _decode_base64url(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")


def _address(value: str) -> str:
    return parseaddr(value)[1] or value.strip()


def _internal_date(raw: Mapping[str, Any]) -> int:
    try:
        return int(raw.get("internalDate") or 0)
    except (TypeError, ValueError) as exc:
        raise ProviderUnavailableError("Gmail 返回了无效的邮件时间") from exc


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ProviderUnavailableError(f"Google API 响应缺少字段：{field}")
    return value


def _validate_limit(limit: int, *, maximum: int = 100) -> None:
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit 必须在 1 到 {maximum} 之间")


def _gmail_search_query(criteria: EmailSearchCriteria) -> str | None:
    parts: list[str] = []
    if criteria.folder is EmailSearchFolder.INBOX:
        parts.append("in:inbox")
    if criteria.since is not None:
        parts.append(f"after:{criteria.since.date().isoformat().replace('-', '/')}")
    if criteria.query:
        parts.append(f"({criteria.query})")
    if criteria.keywords:
        terms = " OR ".join(f'"{value.replace(chr(34), "")}"' for value in criteria.keywords)
        parts.append(f"({terms})")
    return " ".join(parts) or None


def _map_http_error(error: HttpError) -> Exception:
    status = int(getattr(error.resp, "status", 0) or 0)
    if status == 401:
        return ProviderAuthenticationError("Google API 认证失败")
    if status == 403:
        return ProviderPermissionError("Google API 权限不足")
    if status == 404:
        return ProviderNotFoundError("Google API 资源不存在")
    if status == 429:
        return ProviderRateLimitError("Google API 请求受限")
    if status in {408, 504}:
        return ProviderTimeoutError("Google API 请求超时")
    return ProviderUnavailableError("Google API 请求失败")
