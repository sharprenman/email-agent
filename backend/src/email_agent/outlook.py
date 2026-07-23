"""基于 Microsoft Graph 的 Outlook 邮件 Provider。"""

import asyncio
import base64
import binascii
import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import httpx

from .config import Settings
from .contracts import (
    Attachment,
    Contact,
    EmailMessage,
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
)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = (
    "offline_access",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Contacts.Read",
    "https://graph.microsoft.com/Calendars.ReadWrite",
)

SUMMARY_FIELDS = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "bodyPreview,isRead,hasAttachments"
)
FULL_FIELDS = SUMMARY_FIELDS + ",body,internetMessageHeaders"


class OutlookProvider:
    """把 Microsoft Graph 响应转换为统一邮件领域契约。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        access_token: str | None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        read_retries: int = 2,
    ) -> None:
        self._client = client
        self._access_token = access_token
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._read_retries = read_retries
        self._refresh_lock = asyncio.Lock()

    @property
    def capabilities(self) -> ProviderCapabilities:
        """明确 Outlook 首期能力及与 Gmail 的差异。"""
        return ProviderCapabilities(
            provider=ProviderName.OUTLOOK,
            attachments=True,
            contacts=True,
            calendar=False,
            unsubscribe_headers=True,
        )

    async def get_identity(self) -> MailboxIdentity:
        """读取当前 Microsoft 账号身份。"""
        result = await self._request(
            "GET",
            "/me",
            params={"$select": "displayName,mail,userPrincipalName"},
        )
        email = str(result.get("mail") or result.get("userPrincipalName") or "").strip()
        if not email:
            raise ProviderUnavailableError("Microsoft Graph 响应缺少邮箱地址")
        return MailboxIdentity(email=email, display_name=_optional_string(result, "displayName"))

    async def read_inbox(
        self,
        *,
        limit: int,
        unread_only: bool = False,
    ) -> Sequence[EmailSummary]:
        """按接收时间倒序读取 Outlook 收件箱。"""
        params = _message_list_params(limit)
        if unread_only:
            params["$filter"] = "isRead eq false"
            params.pop("$orderby")
        return await self._list_summaries("/me/mailFolders/inbox/messages", limit, params=params)

    async def search_emails(self, *, query: str, limit: int) -> Sequence[EmailSummary]:
        """使用 Microsoft Graph 邮件搜索查询。"""
        if not query.strip():
            raise ValueError("Outlook 搜索条件不能为空")
        params = {
            "$search": f'"{query.strip()}"',
            "$top": str(limit),
            "$select": SUMMARY_FIELDS,
        }
        return await self._list_summaries(
            "/me/messages",
            limit,
            params=params,
            headers={"ConsistencyLevel": "eventual"},
        )

    async def get_email(self, email_id: str) -> EmailMessage:
        """读取完整 Outlook 邮件正文和标准化头信息。"""
        raw = await self._get_message(email_id)
        return _parse_message(raw)

    async def get_sent_emails(self, *, limit: int) -> Sequence[EmailSummary]:
        """读取 Outlook 已发送文件夹。"""
        return await self._list_summaries(
            "/me/mailFolders/sentitems/messages",
            limit,
            params=_message_list_params(limit),
        )

    async def get_unanswered_emails(self, *, limit: int) -> Sequence[EmailSummary]:
        """返回最后一封消息来自对方的 Outlook 会话。"""
        _validate_limit(limit)
        identity = (await self.get_identity()).email.casefold()
        candidates = await self._list_raw(
            "/me/mailFolders/inbox/messages",
            min(limit * 3, 100),
            params=_message_list_params(min(limit * 3, 100)),
        )
        conversation_ids = list(
            dict.fromkeys(
                item.get("conversationId") for item in candidates if item.get("conversationId")
            )
        )
        results: list[EmailSummary] = []
        for conversation_id in conversation_ids:
            escaped_id = str(conversation_id).replace("'", "''")
            messages = await self._list_raw(
                "/me/messages",
                50,
                params={
                    "$filter": f"conversationId eq '{escaped_id}'",
                    "$top": "50",
                    "$select": SUMMARY_FIELDS,
                },
            )
            if not messages:
                continue
            latest = _parse_summary(max(messages, key=_received_at))
            if latest.sender.casefold() != identity:
                results.append(latest)
            if len(results) == limit:
                break
        return results

    async def list_attachments(self, email_id: str) -> Sequence[Attachment]:
        """列出 Outlook 邮件附件元数据，不下载附件正文。"""
        _validate_email_id(email_id)
        items = await self._list_collection(
            f"/me/messages/{email_id}/attachments",
            100,
            params={"$top": "100", "$select": "id,name,contentType,size"},
        )
        return [
            Attachment(
                id=_required_string(item, "id"),
                email_id=email_id,
                filename=_required_string(item, "name"),
                content_type=str(item.get("contentType") or "application/octet-stream"),
                size_bytes=int(item.get("size") or 0),
            )
            for item in items
        ]

    async def download_attachment(self, email_id: str, attachment_id: str) -> bytes:
        """下载 Outlook 文件附件正文；拒绝项目附件和云引用附件。"""
        _validate_email_id(email_id)
        _validate_email_id(attachment_id)
        result = await self._request(
            "GET",
            f"/me/messages/{email_id}/attachments/{attachment_id}",
        )
        if result.get("@odata.type") != "#microsoft.graph.fileAttachment":
            raise ProviderUnavailableError("Outlook 附件不是可解析的文件附件")
        data = str(result.get("contentBytes") or "")
        if not data:
            raise ProviderUnavailableError("Outlook 附件正文为空")
        try:
            return base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ProviderUnavailableError("Outlook 返回了无效附件正文") from exc

    async def list_contacts(self, *, limit: int) -> Sequence[Contact]:
        """读取 Outlook 默认联系人文件夹。"""
        _validate_limit(limit, maximum=1000)
        items = await self._list_collection(
            "/me/contacts",
            limit,
            params={"$top": str(min(limit, 1000)), "$select": "displayName,emailAddresses"},
        )
        contacts: list[Contact] = []
        for item in items:
            addresses = item.get("emailAddresses") or []
            if not addresses:
                continue
            address = str(addresses[0].get("address") or "").strip()
            if not address:
                continue
            contacts.append(
                Contact(email=address, display_name=_optional_string(item, "displayName"))
            )
        return contacts[:limit]

    async def send_email(self, request: SendEmailRequest, *, idempotency_key: str) -> str:
        """创建可识别的草稿后发送；持久化幂等由上层服务负责。"""
        if not idempotency_key.strip():
            raise ValueError("发送邮件必须提供幂等键")
        message = _send_payload(
            request,
            idempotency_key,
            include_idempotency_header=request.reply_to_email_id is None,
        )
        if request.reply_to_email_id:
            _validate_email_id(request.reply_to_email_id)
            draft = await self._request(
                "POST",
                f"/me/messages/{request.reply_to_email_id}/createReply",
                json=None,
                retry_read=False,
            )
            draft_id = _required_string(draft, "id")
            await self._request(
                "PATCH",
                f"/me/messages/{draft_id}",
                json=message,
                retry_read=False,
            )
        else:
            draft = await self._request("POST", "/me/messages", json=message, retry_read=False)
            draft_id = _required_string(draft, "id")
        await self._request(
            "POST",
            f"/me/messages/{draft_id}/send",
            json=None,
            retry_read=False,
        )
        return draft_id

    async def mark_read(self, email_id: str, *, idempotency_key: str) -> None:
        """将 Outlook 邮件设置为已读。"""
        _validate_email_id(email_id)
        if not idempotency_key.strip():
            raise ValueError("修改邮件状态必须提供幂等键")
        await self._request(
            "PATCH",
            f"/me/messages/{email_id}",
            json={"isRead": True},
            retry_read=False,
        )

    async def aclose(self) -> None:
        """关闭 Provider 持有的 HTTP 连接池。"""
        await self._client.aclose()

    async def request_graph(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        retry_read: bool = True,
    ) -> Mapping[str, Any]:
        """供同一认证上下文中的 Microsoft 适配器复用 Graph 请求。"""
        return await self._request(
            method,
            url,
            params=params,
            headers=headers,
            json=json,
            retry_read=retry_read,
        )

    async def _get_message(self, email_id: str) -> Mapping[str, Any]:
        _validate_email_id(email_id)
        return await self._request(
            "GET",
            f"/me/messages/{email_id}",
            params={"$select": FULL_FIELDS},
            headers={"Prefer": 'outlook.body-content-type="html"'},
        )

    async def _list_summaries(
        self,
        path: str,
        limit: int,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
    ) -> Sequence[EmailSummary]:
        return [
            _parse_summary(item)
            for item in await self._list_raw(path, limit, params=params, headers=headers)
        ]

    async def _list_raw(
        self,
        path: str,
        limit: int,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
    ) -> list[Mapping[str, Any]]:
        _validate_limit(limit)
        return await self._list_collection(path, limit, params=params, headers=headers)

    async def _list_collection(
        self,
        path: str,
        limit: int,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
    ) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        next_url: str | None = path
        next_params: Mapping[str, str] | None = params
        while next_url and len(items) < limit:
            response = await self._request(
                "GET",
                next_url,
                params=next_params,
                headers=headers,
            )
            items.extend(response.get("value", []))
            next_url = _optional_string(response, "@odata.nextLink")
            next_params = None
        return items[:limit]

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        retry_read: bool = True,
    ) -> Mapping[str, Any]:
        await self._ensure_access_token()
        retries_remaining = self._read_retries if method == "GET" and retry_read else 0
        refreshed = False
        while True:
            request_headers = {"Authorization": f"Bearer {self._access_token}", **(headers or {})}
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers=request_headers,
                    json=json,
                )
            except httpx.TimeoutException as exc:
                if retries_remaining:
                    retries_remaining -= 1
                    continue
                raise ProviderTimeoutError("Microsoft Graph 请求超时") from exc
            except httpx.RequestError as exc:
                if retries_remaining:
                    retries_remaining -= 1
                    continue
                raise ProviderUnavailableError("Microsoft Graph 暂时不可用") from exc

            if response.status_code == 401 and not refreshed and self._refresh_token:
                await self._refresh_access_token(failed_access_token=self._access_token)
                refreshed = True
                continue
            if response.status_code in {429, 500, 502, 503} and retries_remaining:
                retries_remaining -= 1
                continue
            if response.is_error:
                raise _map_http_error(response.status_code)
            if response.status_code == 204 or not response.content:
                return {}
            payload = response.json()
            if not isinstance(payload, dict):
                raise ProviderUnavailableError("Microsoft Graph 返回了无效 JSON")
            return payload

    async def _ensure_access_token(self) -> None:
        if self._access_token:
            return
        await self._refresh_access_token()

    async def _refresh_access_token(self, *, failed_access_token: str | None = None) -> None:
        if not all((self._tenant_id, self._client_id, self._client_secret, self._refresh_token)):
            raise ProviderAuthenticationError("Microsoft OAuth 凭证不完整")
        async with self._refresh_lock:
            if failed_access_token is None and self._access_token:
                return
            if failed_access_token is not None and self._access_token != failed_access_token:
                return
            try:
                response = await self._client.post(
                    f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "scope": " ".join(GRAPH_SCOPES),
                    },
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError("Microsoft OAuth 请求超时") from exc
            except httpx.RequestError as exc:
                raise ProviderUnavailableError("Microsoft OAuth 暂时不可用") from exc
            if response.is_error:
                raise ProviderAuthenticationError("Microsoft OAuth 凭证刷新失败")
            payload = response.json()
            access_token = str(payload.get("access_token") or "").strip()
            if not access_token:
                raise ProviderAuthenticationError("Microsoft OAuth 响应缺少访问令牌")
            self._access_token = access_token
            if payload.get("refresh_token"):
                self._refresh_token = str(payload["refresh_token"])


def build_outlook_provider(settings: Settings) -> OutlookProvider:
    """使用环境中的 Microsoft OAuth 凭证创建 Outlook Provider。"""
    required = {
        "MICROSOFT_CLIENT_ID": settings.microsoft_client_id,
        "MICROSOFT_CLIENT_SECRET": settings.microsoft_client_secret,
        "MICROSOFT_REFRESH_TOKEN": settings.microsoft_refresh_token,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ProviderAuthenticationError("缺少 Microsoft OAuth 配置：" + ", ".join(missing))
    client = httpx.AsyncClient(
        base_url=GRAPH_BASE_URL,
        timeout=settings.provider_timeout_seconds,
        follow_redirects=False,
    )
    return OutlookProvider(
        client,
        access_token=(
            settings.microsoft_access_token.get_secret_value()
            if settings.microsoft_access_token is not None
            else None
        ),
        tenant_id=settings.microsoft_tenant_id,
        client_id=settings.microsoft_client_id,
        client_secret=settings.microsoft_client_secret.get_secret_value(),
        refresh_token=settings.microsoft_refresh_token.get_secret_value(),
    )


def _message_list_params(limit: int) -> dict[str, str]:
    _validate_limit(limit)
    return {
        "$top": str(limit),
        "$select": SUMMARY_FIELDS,
        "$orderby": "receivedDateTime desc",
    }


def _parse_message(raw: Mapping[str, Any]) -> EmailMessage:
    summary = _parse_summary(raw)
    body = raw.get("body") or {}
    content = str(body.get("content") or "")
    is_html = str(body.get("contentType") or "").casefold() == "html"
    headers = {
        str(item.get("name") or "").casefold(): str(item.get("value") or "")
        for item in raw.get("internetMessageHeaders", []) or []
        if item.get("name")
    }
    return EmailMessage(
        **summary.model_dump(),
        body_text="" if is_html else content,
        body_html=content if is_html else None,
        headers=headers,
    )


def _parse_summary(raw: Mapping[str, Any]) -> EmailSummary:
    return EmailSummary(
        id=_required_string(raw, "id"),
        thread_id=_optional_string(raw, "conversationId"),
        subject=str(raw.get("subject") or ""),
        sender=_email_address(raw.get("from")) or "unknown",
        recipients=tuple(
            address
            for address in (
                *(_email_address(item) for item in raw.get("toRecipients", []) or []),
                *(_email_address(item) for item in raw.get("ccRecipients", []) or []),
            )
            if address
        ),
        sent_at=_received_at(raw),
        snippet=str(raw.get("bodyPreview") or ""),
        is_read=bool(raw.get("isRead")),
        has_attachments=bool(raw.get("hasAttachments")),
    )


def _send_payload(
    request: SendEmailRequest,
    idempotency_key: str,
    *,
    include_idempotency_header: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "subject": request.subject,
        "body": {"contentType": "Text", "content": request.body},
        "toRecipients": [_recipient(address) for address in request.to],
    }
    if include_idempotency_header:
        payload["internetMessageHeaders"] = [
            {
                "name": "X-Email-Agent-Idempotency-Key",
                "value": hashlib.sha256(idempotency_key.encode()).hexdigest(),
            }
        ]
    if request.cc:
        payload["ccRecipients"] = [_recipient(address) for address in request.cc]
    if request.bcc:
        payload["bccRecipients"] = [_recipient(address) for address in request.bcc]
    return payload


def _recipient(address: str) -> dict[str, dict[str, str]]:
    return {"emailAddress": {"address": address}}


def _email_address(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    email_address = value.get("emailAddress") or {}
    return str(email_address.get("address") or "").strip()


def _received_at(raw: Mapping[str, Any]) -> datetime:
    value = _required_string(raw, "receivedDateTime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderUnavailableError("Microsoft Graph 返回了无效邮件时间") from exc
    if parsed.tzinfo is None:
        raise ProviderUnavailableError("Microsoft Graph 邮件时间缺少时区")
    return parsed


def _optional_string(payload: Mapping[str, Any], field: str) -> str | None:
    value = str(payload.get(field) or "").strip()
    return value or None


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = _optional_string(payload, field)
    if value is None:
        raise ProviderUnavailableError(f"Microsoft Graph 响应缺少字段：{field}")
    return value


def _validate_email_id(email_id: str) -> None:
    if not email_id.strip():
        raise ValueError("邮件 ID 不能为空")


def _validate_limit(limit: int, *, maximum: int = 100) -> None:
    if not 1 <= limit <= maximum:
        raise ValueError(f"limit 必须在 1 到 {maximum} 之间")


def _map_http_error(status: int) -> Exception:
    if status == 401:
        return ProviderAuthenticationError("Microsoft Graph 认证失败")
    if status == 403:
        return ProviderPermissionError("Microsoft Graph 权限不足")
    if status == 404:
        return ProviderNotFoundError("Microsoft Graph 资源不存在")
    if status == 429:
        return ProviderRateLimitError("Microsoft Graph 请求受限")
    if status in {408, 504}:
        return ProviderTimeoutError("Microsoft Graph 请求超时")
    return ProviderUnavailableError("Microsoft Graph 请求失败")
