"""附件安全文本提取与退订确定性执行工具。"""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import threading
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import Field
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .calendar import ApprovalAction, ApprovalService
from .contracts import (
    Attachment,
    ContractModel,
    EmailMessage,
    MailProvider,
    ProviderError,
    SendEmailRequest,
)

ONE_CLICK_VALUE = "List-Unsubscribe=One-Click"
MAX_PDF_PAGES = 20
MAX_DOCX_XML_BYTES = 8_000_000
UNSUBSCRIBE_LINK_PATTERN = re.compile(r"(?:unsubscrib|opt[\s_-]?out|退订|取消订阅)", re.I)
HEADER_URI_PATTERN = re.compile(r"<([^<>]+)>")


class AttachmentTextStatus(StrEnum):
    """附件文本提取结果。"""

    EXTRACTED = "extracted"
    SKIPPED = "skipped"
    FAILED = "failed"


class AttachmentTextResult(ContractModel):
    """附件解析后的安全结果。"""

    attachment_id: str
    filename: str
    status: AttachmentTextStatus
    text: str = ""
    truncated: bool = False
    reason: str | None = None


class AttachmentTextService:
    """仅在内存中下载并解析白名单附件。"""

    def __init__(
        self,
        *,
        max_attachment_bytes: int,
        timeout_seconds: float = 10,
        max_text_chars: int = 20_000,
        extractor: Callable[[str, bytes], str] | None = None,
    ) -> None:
        if max_attachment_bytes <= 0 or timeout_seconds <= 0 or max_text_chars <= 0:
            raise ValueError("附件解析限制必须大于零")
        self._max_bytes = max_attachment_bytes
        self._timeout = timeout_seconds
        self._max_chars = max_text_chars
        self._extractor = extractor or _extract_attachment_text

    async def extract(
        self,
        provider: MailProvider,
        attachment: Attachment,
    ) -> AttachmentTextResult:
        """校验元数据、下载正文并在超时边界内提取文本。"""
        filename = _safe_filename(attachment.filename)
        suffix = Path(filename).suffix.casefold()
        if not _is_supported_attachment(suffix, attachment.content_type):
            return _attachment_result(
                attachment,
                filename,
                AttachmentTextStatus.SKIPPED,
                "附件类型不受支持或 MIME 与扩展名不一致",
            )
        if attachment.size_bytes > self._max_bytes:
            return _attachment_result(
                attachment,
                filename,
                AttachmentTextStatus.SKIPPED,
                "附件超过大小限制",
            )

        try:
            content = await provider.download_attachment(attachment.email_id, attachment.id)
            if len(content) > self._max_bytes:
                return _attachment_result(
                    attachment,
                    filename,
                    AttachmentTextStatus.SKIPPED,
                    "附件实际内容超过大小限制",
                )
            text = await asyncio.wait_for(
                asyncio.to_thread(self._extractor, suffix, content),
                timeout=self._timeout,
            )
        except TimeoutError:
            return _attachment_result(
                attachment,
                filename,
                AttachmentTextStatus.FAILED,
                "附件解析超时",
            )
        except (ProviderError, PyPdfError, ValueError, OSError, zipfile.BadZipFile):
            return _attachment_result(
                attachment,
                filename,
                AttachmentTextStatus.FAILED,
                "附件解析失败",
            )

        normalized = text.replace("\x00", "").strip()
        truncated = len(normalized) > self._max_chars
        return AttachmentTextResult(
            attachment_id=attachment.id,
            filename=filename,
            status=AttachmentTextStatus.EXTRACTED,
            text=normalized[: self._max_chars],
            truncated=truncated,
        )


class UnsubscribeMethod(StrEnum):
    """退订候选的执行方式。"""

    ONE_CLICK = "one_click"
    MAILTO = "mailto"
    WEBSITE = "website"
    UNKNOWN = "unknown"


class UnsubscribeSource(StrEnum):
    """退订候选来源。"""

    HEADER = "header"
    BODY = "body"
    NONE = "none"


class UnsubscribeCandidate(ContractModel):
    """从邮件中确定性发现的退订候选。"""

    method: UnsubscribeMethod
    source: UnsubscribeSource
    target: str | None = Field(default=None, max_length=4096)
    address: str | None = Field(default=None, max_length=320)
    subject: str | None = Field(default=None, max_length=998)
    body: str | None = Field(default=None, max_length=10_000)
    dkim_evidence: bool = False

    @property
    def fingerprint(self) -> str:
        """生成不泄露退订令牌的稳定候选指纹。"""
        payload = self.model_dump_json(exclude={"source"}, exclude_none=True)
        return hashlib.sha256(payload.encode()).hexdigest()


class UnsubscribeState(StrEnum):
    """退订副作用状态。"""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class UnsubscribeRecord(ContractModel):
    """不保存原始退订地址的最小审计记录。"""

    state: UnsubscribeState
    method: UnsubscribeMethod
    target_hash: str
    idempotency_hash: str
    updated_at: datetime
    evidence_hash: str | None = None
    status_code: int | None = None


class UnsubscribeResultStatus(StrEnum):
    """面向 Agent 的退订执行结果。"""

    CONFIRMED = "confirmed"
    REQUEST_SENT = "request_sent"
    MANUAL_REQUIRED = "manual_required"
    UNSUPPORTED = "unsupported"
    ALREADY_SUBMITTED = "already_submitted"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class UnsubscribeResult(ContractModel):
    """一次退订候选执行结果。"""

    method: UnsubscribeMethod
    status: UnsubscribeResultStatus
    target: str | None = None
    status_code: int | None = None
    message: str


class JsonUnsubscribeStateStore:
    """适用于单进程私有部署的原子 JSON 状态存储。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def get(self, user_id: str, target_hash: str) -> UnsubscribeRecord | None:
        """读取用户范围内的候选状态。"""
        with self._lock:
            payload = self._read()
            raw = payload["records"].get(_state_key(user_id, target_hash))
            return UnsubscribeRecord.model_validate(raw) if raw else None

    def begin(
        self,
        user_id: str,
        candidate: UnsubscribeCandidate,
        idempotency_key: str,
    ) -> tuple[bool, UnsubscribeRecord]:
        """持久化执行占位，原子阻止成功、处理中或不确定请求重放。"""
        record = _new_record(candidate, idempotency_key, UnsubscribeState.PENDING)
        key = _state_key(user_id, candidate.fingerprint)
        with self._lock:
            payload = self._read()
            existing_raw = payload["records"].get(key)
            if existing_raw:
                existing = UnsubscribeRecord.model_validate(existing_raw)
                if (
                    existing.state is not UnsubscribeState.FAILED
                    or existing.idempotency_hash == record.idempotency_hash
                ):
                    return False, existing
            payload["records"][key] = record.model_dump(mode="json")
            self._write(payload)
            return True, record

    def finish(
        self,
        user_id: str,
        candidate: UnsubscribeCandidate,
        idempotency_key: str,
        state: UnsubscribeState,
        *,
        evidence_hash: str | None = None,
        status_code: int | None = None,
    ) -> UnsubscribeRecord:
        """保存最终结果；只允许当前幂等请求完成自己的占位。"""
        record = _new_record(
            candidate,
            idempotency_key,
            state,
            evidence_hash=evidence_hash,
            status_code=status_code,
        )
        key = _state_key(user_id, candidate.fingerprint)
        with self._lock:
            payload = self._read()
            current_raw = payload["records"].get(key)
            current = UnsubscribeRecord.model_validate(current_raw) if current_raw else None
            if current is None or current.idempotency_hash != record.idempotency_hash:
                raise RuntimeError("退订状态与当前幂等请求不一致")
            payload["records"][key] = record.model_dump(mode="json")
            self._write(payload)
        return record

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 1, "records": {}}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("退订状态文件不可读，已默认拒绝执行") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("records"), dict)
        ):
            raise RuntimeError("退订状态文件格式无效，已默认拒绝执行")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self._path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


class UnsubscribeService:
    """审批后执行 one-click 或 mailto，并阻止重复副作用。"""

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        store: JsonUnsubscribeStateStore,
        http_client: httpx.AsyncClient,
        mail_provider: MailProvider | None = None,
    ) -> None:
        self._approvals = approvals
        self._store = store
        self._client = http_client
        self._mail = mail_provider

    async def execute(
        self,
        candidate: UnsubscribeCandidate,
        *,
        user_id: str,
        approval_token: str,
        idempotency_key: str,
    ) -> UnsubscribeResult:
        """执行单个候选；网站链接永远只返回人工处理。"""
        if candidate.method is UnsubscribeMethod.WEBSITE:
            return UnsubscribeResult(
                method=candidate.method,
                status=UnsubscribeResultStatus.MANUAL_REQUIRED,
                target=candidate.target,
                message="网站退订需要人工打开链接完成",
            )
        if candidate.method is UnsubscribeMethod.UNKNOWN:
            return UnsubscribeResult(
                method=candidate.method,
                status=UnsubscribeResultStatus.UNSUPPORTED,
                message="未发现可执行的退订方式",
            )
        if candidate.method is UnsubscribeMethod.ONE_CLICK:
            _validated_one_click_url(candidate)
        elif candidate.address is None:
            raise ValueError("mailto 退订地址无效")

        action = (
            ApprovalAction.UNSUBSCRIBE_ONE_CLICK
            if candidate.method is UnsubscribeMethod.ONE_CLICK
            else ApprovalAction.UNSUBSCRIBE_MAILTO
        )
        payload = candidate.model_dump(mode="json")
        self._approvals.consume(
            approval_token,
            user_id=user_id,
            action=action,
            target_id=candidate.fingerprint,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        started, _ = await asyncio.to_thread(
            self._store.begin,
            user_id,
            candidate,
            idempotency_key,
        )
        if not started:
            return UnsubscribeResult(
                method=candidate.method,
                status=UnsubscribeResultStatus.ALREADY_SUBMITTED,
                message="该退订目标已经处理或正在处理中",
            )

        if candidate.method is UnsubscribeMethod.ONE_CLICK:
            return await self._execute_one_click(
                candidate,
                user_id=user_id,
                idempotency_key=idempotency_key,
            )
        return await self._execute_mailto(
            candidate,
            user_id=user_id,
            idempotency_key=idempotency_key,
        )

    async def execute_many(
        self,
        requests: Sequence[tuple[UnsubscribeCandidate, str, str]],
        *,
        user_id: str,
    ) -> tuple[UnsubscribeResult, ...]:
        """逐项执行审批后的候选，保留部分成功结果。"""
        results = []
        for candidate, approval_token, idempotency_key in requests:
            results.append(
                await self.execute(
                    candidate,
                    user_id=user_id,
                    approval_token=approval_token,
                    idempotency_key=idempotency_key,
                )
            )
        return tuple(results)

    async def _execute_one_click(
        self,
        candidate: UnsubscribeCandidate,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> UnsubscribeResult:
        target = _validated_one_click_url(candidate)
        try:
            response = await self._client.post(
                target,
                data={"List-Unsubscribe": "One-Click"},
                headers={"Accept": "text/plain, application/json"},
                follow_redirects=False,
            )
        except (httpx.TimeoutException, httpx.RequestError):
            await self._finish(
                user_id,
                candidate,
                idempotency_key,
                UnsubscribeState.UNCERTAIN,
            )
            return UnsubscribeResult(
                method=candidate.method,
                status=UnsubscribeResultStatus.UNCERTAIN,
                message="退订请求结果不确定，为避免重复操作不会自动重试",
            )

        evidence = hashlib.sha256(
            f"{response.status_code}:".encode() + response.content[:1024]
        ).hexdigest()
        succeeded = 200 <= response.status_code < 300
        await self._finish(
            user_id,
            candidate,
            idempotency_key,
            UnsubscribeState.SUCCEEDED if succeeded else UnsubscribeState.FAILED,
            evidence_hash=evidence,
            status_code=response.status_code,
        )
        return UnsubscribeResult(
            method=candidate.method,
            status=(
                UnsubscribeResultStatus.CONFIRMED if succeeded else UnsubscribeResultStatus.FAILED
            ),
            status_code=response.status_code,
            message="退订请求已被服务器接受" if succeeded else "退订服务器拒绝了请求",
        )

    async def _execute_mailto(
        self,
        candidate: UnsubscribeCandidate,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> UnsubscribeResult:
        if self._mail is None or candidate.address is None:
            await self._finish(
                user_id,
                candidate,
                idempotency_key,
                UnsubscribeState.FAILED,
            )
            return UnsubscribeResult(
                method=candidate.method,
                status=UnsubscribeResultStatus.FAILED,
                message="当前邮箱未配置 mailto 退订发送能力",
            )
        request = SendEmailRequest(
            to=(candidate.address,),
            subject=candidate.subject or "unsubscribe",
            body=candidate.body or "unsubscribe",
        )
        try:
            message_id = await self._mail.send_email(request, idempotency_key=idempotency_key)
        except ProviderError:
            await self._finish(
                user_id,
                candidate,
                idempotency_key,
                UnsubscribeState.FAILED,
            )
            return UnsubscribeResult(
                method=candidate.method,
                status=UnsubscribeResultStatus.FAILED,
                message="mailto 退订邮件发送失败",
            )
        evidence = hashlib.sha256(message_id.encode()).hexdigest()
        await self._finish(
            user_id,
            candidate,
            idempotency_key,
            UnsubscribeState.SUCCEEDED,
            evidence_hash=evidence,
        )
        return UnsubscribeResult(
            method=candidate.method,
            status=UnsubscribeResultStatus.REQUEST_SENT,
            message="mailto 退订邮件已发送",
        )

    async def _finish(
        self,
        user_id: str,
        candidate: UnsubscribeCandidate,
        idempotency_key: str,
        state: UnsubscribeState,
        *,
        evidence_hash: str | None = None,
        status_code: int | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._store.finish,
            user_id,
            candidate,
            idempotency_key,
            state,
            evidence_hash=evidence_hash,
            status_code=status_code,
        )


def discover_unsubscribe(message: EmailMessage) -> tuple[UnsubscribeCandidate, ...]:
    """解析标准退订头，并在缺少标准头时回退到正文链接。"""
    headers = {str(key).casefold(): str(value) for key, value in message.headers.items()}
    list_header = headers.get("list-unsubscribe", "")
    post_header = headers.get("list-unsubscribe-post", "").strip()
    dkim_evidence = _has_dkim_evidence(headers)
    candidates: list[UnsubscribeCandidate] = []

    for raw_uri in HEADER_URI_PATTERN.findall(list_header):
        uri = raw_uri.strip()
        if uri.casefold().startswith("mailto:"):
            candidate = _parse_mailto(uri)
            if candidate is not None:
                candidates.append(candidate)
        elif urlparse(uri).scheme.casefold() == "https":
            is_one_click = post_header == ONE_CLICK_VALUE and dkim_evidence
            candidates.append(
                UnsubscribeCandidate(
                    method=(
                        UnsubscribeMethod.ONE_CLICK if is_one_click else UnsubscribeMethod.WEBSITE
                    ),
                    source=UnsubscribeSource.HEADER,
                    target=uri,
                    dkim_evidence=dkim_evidence,
                )
            )

    if not candidates:
        for link in _body_links(message):
            candidates.append(
                UnsubscribeCandidate(
                    method=UnsubscribeMethod.WEBSITE,
                    source=UnsubscribeSource.BODY,
                    target=link,
                )
            )
    if not candidates:
        candidates.append(
            UnsubscribeCandidate(
                method=UnsubscribeMethod.UNKNOWN,
                source=UnsubscribeSource.NONE,
            )
        )
    unique = {candidate.fingerprint: candidate for candidate in candidates}
    return tuple(unique.values())


def _extract_attachment_text(suffix: str, content: bytes) -> str:
    if suffix in {".txt", ".md"}:
        return content.decode("utf-8-sig", errors="replace")
    if suffix in {".html", ".htm"}:
        parser = _VisibleTextParser()
        parser.feed(content.decode("utf-8-sig", errors="replace"))
        return parser.text
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("PDF 文件签名无效")
        reader = PdfReader(BytesIO(content), strict=True)
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("PDF 页数超过限制")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        with zipfile.ZipFile(BytesIO(content)) as archive:
            try:
                document = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise ValueError("DOCX 缺少正文") from exc
            if document.file_size > MAX_DOCX_XML_BYTES:
                raise ValueError("DOCX 解压正文超过限制")
            xml = archive.read(document).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", xml)
    raise ValueError("附件类型不受支持")


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._hidden = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "\n".join(part.strip() for part in self._parts if part.strip())


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = next((value for name, value in attrs if name.casefold() == "href"), None)
        if href and UNSUBSCRIBE_LINK_PATTERN.search(href):
            self.links.append(href)


def _body_links(message: EmailMessage) -> tuple[str, ...]:
    links: list[str] = []
    if message.body_html:
        parser = _LinkParser()
        parser.feed(message.body_html)
        links.extend(parser.links)
    for match in re.findall(r"https://[^\s<>'\"]+", message.body_text):
        if UNSUBSCRIBE_LINK_PATTERN.search(match):
            links.append(match.rstrip(".,);"))
    return tuple(dict.fromkeys(link for link in links if _is_public_https_shape(link)))


def _parse_mailto(uri: str) -> UnsubscribeCandidate | None:
    parsed = urlparse(uri)
    address = unquote(parsed.path).strip()
    local, separator, domain = address.partition("@")
    if (
        not separator
        or not local
        or not domain
        or any(character.isspace() for character in address)
    ):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    return UnsubscribeCandidate(
        method=UnsubscribeMethod.MAILTO,
        source=UnsubscribeSource.HEADER,
        target=uri,
        address=address,
        subject=(query.get("subject") or [None])[0],
        body=(query.get("body") or [None])[0],
    )


def _has_dkim_evidence(headers: dict[str, str]) -> bool:
    authentication = headers.get("authentication-results", "").casefold()
    signature = headers.get("dkim-signature", "").casefold()
    signed_headers_match = re.search(r"(?:^|;)\s*h=([^;]+)", signature)
    if "dkim=pass" not in authentication or signed_headers_match is None:
        return False
    signed_headers = {
        value.strip() for value in signed_headers_match.group(1).split(":") if value.strip()
    }
    return {"list-unsubscribe", "list-unsubscribe-post"} <= signed_headers


def _validated_one_click_url(candidate: UnsubscribeCandidate) -> str:
    if (
        candidate.target is None
        or not candidate.dkim_evidence
        or not _is_public_https_shape(candidate.target)
    ):
        raise ValueError("one-click 退订地址或 DKIM 证据无效")
    return candidate.target


def _is_public_https_shape(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username:
        return False
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return address.is_global


def _is_supported_attachment(suffix: str, content_type: str) -> bool:
    normalized = content_type.partition(";")[0].strip().casefold()
    allowed = {
        ".txt": {"text/plain"},
        ".md": {"text/markdown", "text/plain"},
        ".html": {"text/html"},
        ".htm": {"text/html"},
        ".pdf": {"application/pdf"},
        ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    }
    return normalized in allowed.get(suffix, set())


def _safe_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    safe = PurePosixPath(normalized).name.strip()
    return safe or "attachment"


def _attachment_result(
    attachment: Attachment,
    filename: str,
    status: AttachmentTextStatus,
    reason: str,
) -> AttachmentTextResult:
    return AttachmentTextResult(
        attachment_id=attachment.id,
        filename=filename,
        status=status,
        reason=reason,
    )


def _new_record(
    candidate: UnsubscribeCandidate,
    idempotency_key: str,
    state: UnsubscribeState,
    *,
    evidence_hash: str | None = None,
    status_code: int | None = None,
) -> UnsubscribeRecord:
    if not idempotency_key.strip():
        raise ValueError("退订操作必须提供幂等键")
    return UnsubscribeRecord(
        state=state,
        method=candidate.method,
        target_hash=candidate.fingerprint,
        idempotency_hash=hashlib.sha256(idempotency_key.encode()).hexdigest(),
        updated_at=datetime.now(UTC),
        evidence_hash=evidence_hash,
        status_code=status_code,
    )


def _state_key(user_id: str, target_hash: str) -> str:
    if not user_id.strip():
        raise ValueError("用户 ID 不能为空")
    return hashlib.sha256(f"{user_id}\0{target_hash}".encode()).hexdigest()
