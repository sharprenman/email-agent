"""阿里邮箱开放平台邮件 Provider。"""

import asyncio
import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ...contracts import (
    Attachment,
    Contact,
    EmailMessage,
    EmailSearchCriteria,
    EmailSearchFolder,
    EmailSummary,
    MailboxIdentity,
    ProviderCapabilities,
    ProviderName,
    ProviderUnavailableError,
    SendEmailRequest,
)
from .client import AliMailClient

INBOX_FOLDER_ID = "2"
SENT_FOLDER_ID = "1"
LIST_MESSAGE_SELECT = (
    "id,conversationId,internetMessageId,subject,summary,from,sender,"
    "toRecipients,ccRecipients,folderId,sentDateTime,receivedDateTime,"
    "isRead,hasAttachments,isReplied,sendStatus"
)
FULL_MESSAGE_SELECT = LIST_MESSAGE_SELECT + ",bccRecipients,replyTo,body,internetMessageHeaders"


class AliMailProvider:
    """把阿里邮箱开放平台响应转换为统一邮件领域契约。"""

    def __init__(self, client: AliMailClient, *, email: str) -> None:
        self._client = client
        self._email = email
        self._user_path = f"/v2/users/{quote(email, safe='')}"

    @property
    def capabilities(self) -> ProviderCapabilities:
        """返回阿里邮箱已接入的邮件能力。"""
        return ProviderCapabilities(
            provider=ProviderName.ALIMAIL,
            attachments=True,
            contacts=True,
            calendar=False,
            unsubscribe_headers=True,
        )

    async def get_identity(self) -> MailboxIdentity:
        """返回应用被配置代理的企业邮箱身份。"""
        return MailboxIdentity(email=self._email)

    async def read_inbox(
        self,
        *,
        limit: int,
        unread_only: bool = False,
    ) -> Sequence[EmailSummary]:
        """按时间倒序读取收件箱。"""
        predicate = (lambda item: not bool(item.get("isRead"))) if unread_only else None
        return [
            _parse_summary(item)
            for item in await self._list_folder(INBOX_FOLDER_ID, limit, predicate=predicate)
        ]

    async def search_emails(
        self,
        *,
        criteria: EmailSearchCriteria,
        limit: int,
    ) -> Sequence[EmailSummary]:
        """用受限文件夹扫描执行 Provider 无关搜索，避免泄漏 KQL 差异。"""
        _validate_limit(limit)
        folder_ids = (
            (INBOX_FOLDER_ID,)
            if criteria.folder is EmailSearchFolder.INBOX
            else (INBOX_FOLDER_ID, SENT_FOLDER_ID)
        )
        summaries = [
            _parse_summary(item)
            for folder_id in folder_ids
            for item in await self._list_folder(folder_id, 100)
        ]
        matched = [item for item in summaries if _matches_search(item, criteria)]
        deduplicated = {item.id: item for item in matched}
        return sorted(
            deduplicated.values(),
            key=lambda item: item.sent_at,
            reverse=True,
        )[:limit]

    async def get_email(self, email_id: str) -> EmailMessage:
        """读取完整邮件正文和头信息。"""
        raw = await self._get_message(email_id)
        summary = _parse_summary(raw)
        body = raw.get("body") or {}
        headers = raw.get("internetMessageHeaders") or {}
        return EmailMessage(
            **summary.model_dump(),
            body_text=str(body.get("bodyText") or ""),
            body_html=_optional_string(body, "bodyHtml"),
            headers={
                str(name).casefold(): str(value)
                for name, value in headers.items()
                if str(name).strip()
            },
        )

    async def get_sent_emails(self, *, limit: int) -> Sequence[EmailSummary]:
        """读取已发送文件夹。"""
        return [_parse_summary(item) for item in await self._list_folder(SENT_FOLDER_ID, limit)]

    async def get_unanswered_emails(
        self,
        *,
        limit: int,
        since: datetime | None = None,
    ) -> Sequence[EmailSummary]:
        """从最新一页邮件中读取尚未回复项，避免为凑满数量遍历整个邮箱。"""
        return [
            _parse_summary(item)
            for item in await self._list_folder(
                INBOX_FOLDER_ID,
                limit,
                predicate=lambda item: (
                    not bool(item.get("isReplied"))
                    and (since is None or _message_time(item) >= since)
                ),
                max_pages=1,
            )
        ]

    async def list_attachments(self, email_id: str) -> Sequence[Attachment]:
        """列出邮件附件元数据。"""
        email_id = _validated_id(email_id, "邮件")
        response = await self._client.request(
            "GET",
            f"{self._user_path}/messages/{quote(email_id, safe='')}/attachments",
        )
        return [
            Attachment(
                id=_required_string(item, "id"),
                email_id=email_id,
                filename=_required_string(item, "name"),
                content_type=_attachment_content_type(item),
                size_bytes=int(item.get("size") or 0),
            )
            for item in response.get("attachments", [])
            if isinstance(item, Mapping)
        ]

    async def download_attachment(self, email_id: str, attachment_id: str) -> bytes:
        """创建附件下载会话并读取临时 HTTPS 地址。"""
        email_id = _validated_id(email_id, "邮件")
        attachment_id = _validated_id(attachment_id, "附件")
        location = await self._client.get_download_location(
            f"{self._user_path}/messages/{quote(email_id, safe='')}/attachments/"
            f"{quote(attachment_id, safe='')}/$value",
        )
        return await self._client.download(location)

    async def list_contacts(self, *, limit: int) -> Sequence[Contact]:
        """读取企业共享通讯录根分组联系人。"""
        _validate_limit(limit, maximum=1000)
        contacts: list[Contact] = []
        offset = 0
        while len(contacts) < limit:
            page_size = min(limit - len(contacts), 100)
            response = await self._client.request(
                "GET",
                "/v2/sharedContactFolders/$root/contacts",
                params={"offset": str(offset), "limit": str(page_size)},
            )
            items = response.get("contacts", [])
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                email = str(item.get("email") or "").strip()
                if email:
                    contacts.append(
                        Contact(email=email, display_name=_optional_string(item, "name"))
                    )
            offset += len(items)
            if not items or offset >= int(response.get("total") or 0):
                break
        return contacts[:limit]

    async def send_email(self, request: SendEmailRequest, *, idempotency_key: str) -> str:
        """创建并发送草稿，返回发送后生成的正式邮件 ID。"""
        if not idempotency_key.strip():
            raise ValueError("发送邮件必须提供幂等键")
        internet_message_id = _stable_internet_message_id(
            idempotency_key,
            self._email,
        )
        idempotency_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        headers = {
            "X-Email-Agent-Idempotency-Key": idempotency_digest
        }
        if request.reply_to_email_id:
            original = await self._get_message(request.reply_to_email_id)
            reply_message_id = str(original.get("internetMessageId") or "").strip()
            if reply_message_id:
                headers["In-Reply-To"] = reply_message_id
                headers["References"] = reply_message_id

        message: dict[str, Any] = {
            "internetMessageId": internet_message_id,
            "subject": request.subject,
            "from": {"email": self._email},
            "toRecipients": [_recipient(address) for address in request.to],
            "body": {"bodyText": request.body},
            "internetMessageHeaders": headers,
        }
        if request.cc:
            message["ccRecipients"] = [_recipient(address) for address in request.cc]
        if request.bcc:
            message["bccRecipients"] = [_recipient(address) for address in request.bcc]
        created = await self._client.request(
            "POST",
            f"{self._user_path}/messages",
            json={"message": message},
            retry_read=False,
        )
        raw_message = created.get("message")
        if not isinstance(raw_message, Mapping):
            raise ProviderUnavailableError("阿里邮箱创建草稿响应缺少 message")
        draft_id = _required_string(raw_message, "id")
        await self._client.request(
            "POST",
            f"{self._user_path}/messages/{quote(draft_id, safe='')}/send",
            json={"saveToSentItems": True},
            retry_read=False,
        )
        return await self._resolve_sent_message_id(internet_message_id, idempotency_digest)

    async def mark_read(self, email_id: str, *, idempotency_key: str) -> None:
        """将邮件标记为已读。"""
        email_id = _validated_id(email_id, "邮件")
        if not idempotency_key.strip():
            raise ValueError("修改邮件状态必须提供幂等键")
        await self._client.request(
            "POST",
            f"{self._user_path}/messages/batchUpdate",
            json={"ids": [email_id], "message": {"isRead": True}, "action": "markRead"},
            retry_read=False,
        )

    async def aclose(self) -> None:
        """关闭共享的阿里邮箱 HTTP 客户端。"""
        await self._client.aclose()

    async def _get_message(self, email_id: str) -> Mapping[str, Any]:
        email_id = _validated_id(email_id, "邮件")
        response = await self._client.request(
            "GET",
            f"{self._user_path}/messages/{quote(email_id, safe='')}",
            params={"$select": FULL_MESSAGE_SELECT},
        )
        message = response.get("message")
        if not isinstance(message, Mapping):
            raise ProviderUnavailableError("阿里邮箱邮件响应缺少 message")
        return message

    async def _resolve_sent_message_id(
        self,
        internet_message_id: str,
        idempotency_digest: str,
    ) -> str:
        for attempt in range(3):
            messages = await self._list_folder(SENT_FOLDER_ID, 100)
            for message in messages[:10]:
                message_id = _required_string(message, "id")
                if message.get("internetMessageId") == internet_message_id:
                    return message_id
                detail = await self._get_message(message_id)
                headers = detail.get("internetMessageHeaders")
                if isinstance(headers, Mapping) and any(
                    str(name).casefold() == "x-email-agent-idempotency-key"
                    and str(value) == idempotency_digest
                    for name, value in headers.items()
                ):
                    return message_id
            if attempt < 2:
                await asyncio.sleep(1)
        raise ProviderUnavailableError(
            "阿里邮箱已接受发送请求，但未能确认已发送邮件 ID，请勿直接重试"
        )

    async def _list_folder(
        self,
        folder_id: str,
        limit: int,
        *,
        predicate: Callable[[Mapping[str, Any]], bool] | None = None,
        max_pages: int | None = None,
    ) -> list[Mapping[str, Any]]:
        _validate_limit(limit)
        messages: list[Mapping[str, Any]] = []
        cursor = ""
        pages = 0
        while len(messages) < limit:
            response = await self._client.request(
                "GET",
                f"{self._user_path}/mailFolders/{folder_id}/messages",
                params={
                    "cursor": cursor,
                    "size": "100",
                    "isAscending": "false",
                    "$select": LIST_MESSAGE_SELECT,
                },
            )
            for item in response.get("messages", []):
                if isinstance(item, Mapping) and (predicate is None or predicate(item)):
                    messages.append(item)
                    if len(messages) == limit:
                        break
            pages += 1
            cursor = str(response.get("nextCursor") or "")
            if (
                not response.get("hasMore")
                or not cursor
                or (max_pages is not None and pages >= max_pages)
            ):
                break
        return messages

    async def _collect_pages(
        self,
        path: str,
        limit: int,
        *,
        initial_payload: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        messages: list[Mapping[str, Any]] = []
        payload = dict(initial_payload)
        while len(messages) < limit:
            response = await self._client.request(
                "POST",
                path,
                params={"$select": LIST_MESSAGE_SELECT},
                json=payload,
                retry_read=True,
            )
            messages.extend(
                item for item in response.get("messages", []) if isinstance(item, Mapping)
            )
            cursor = str(response.get("nextCursor") or "")
            if not response.get("hasMore") or not cursor:
                break
            payload["cursor"] = cursor
            payload["size"] = min(limit - len(messages), 100)
        return messages[:limit]


def _parse_summary(raw: Mapping[str, Any]) -> EmailSummary:
    return EmailSummary(
        id=_required_string(raw, "id"),
        thread_id=_optional_string(raw, "conversationId"),
        subject=str(raw.get("subject") or ""),
        sender=_recipient_email(raw.get("from") or raw.get("sender")) or "unknown",
        recipients=tuple(
            address
            for address in (
                *(_recipient_email(item) for item in raw.get("toRecipients", []) or []),
                *(_recipient_email(item) for item in raw.get("ccRecipients", []) or []),
            )
            if address
        ),
        sent_at=_message_time(raw),
        snippet=str(raw.get("summary") or ""),
        is_read=bool(raw.get("isRead")),
        has_attachments=bool(raw.get("hasAttachments")),
    )


def _message_time(raw: Mapping[str, Any]) -> datetime:
    value = str(raw.get("receivedDateTime") or raw.get("sentDateTime") or "").strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderUnavailableError("阿里邮箱返回了无效邮件时间") from exc
    if parsed.tzinfo is None:
        raise ProviderUnavailableError("阿里邮箱邮件时间缺少时区")
    return parsed


def _recipient(address: str) -> dict[str, str]:
    return {"email": address}


def _stable_internet_message_id(idempotency_key: str, sender: str) -> str:
    domain = sender.partition("@")[2] or "localhost"
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
    return f"<email-agent-{digest}@{domain}>"


def _recipient_email(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return str(value.get("email") or "").strip()


def _attachment_content_type(item: Mapping[str, Any]) -> str:
    headers = item.get("extHeaders") or {}
    if isinstance(headers, Mapping):
        for name, value in headers.items():
            if str(name).casefold() == "content-type" and str(value).strip():
                return str(value)
    return "application/octet-stream"


def _optional_string(payload: Mapping[str, Any], field: str) -> str | None:
    value = str(payload.get(field) or "").strip()
    return value or None


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = _optional_string(payload, field)
    if value is None:
        raise ProviderUnavailableError(f"阿里邮箱响应缺少字段：{field}")
    return value


def _validated_id(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} ID 不能为空")
    return value


def _validate_limit(limit: int, *, maximum: int = 100) -> None:
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit 必须在 1 到 {maximum} 之间")


def _matches_search(summary: EmailSummary, criteria: EmailSearchCriteria) -> bool:
    if criteria.since is not None and summary.sent_at < criteria.since:
        return False
    searchable = " ".join(
        (summary.subject, summary.sender, summary.snippet)
    ).casefold()
    if criteria.query and criteria.query.casefold() not in searchable:
        return False
    return not criteria.keywords or any(
        keyword.casefold() in searchable for keyword in criteria.keywords
    )
