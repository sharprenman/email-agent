"""附件安全解析与退订执行测试。"""

import asyncio
import json
import time
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from email_agent.calendar import ApprovalAction, ApprovalService
from email_agent.config import AuthContext
from email_agent.content_tools import (
    AttachmentTextService,
    AttachmentTextStatus,
    JsonUnsubscribeStateStore,
    UnsubscribeCandidate,
    UnsubscribeMethod,
    UnsubscribeResultStatus,
    UnsubscribeService,
    UnsubscribeSource,
    discover_unsubscribe,
)
from email_agent.contracts import Attachment, EmailMessage


def _attachment(
    *,
    filename: str = "说明.txt",
    content_type: str = "text/plain",
    size_bytes: int = 4,
) -> Attachment:
    return Attachment(
        id="attachment-1",
        email_id="mail-1",
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
    )


def _mail_provider(content: bytes = b"text"):
    provider = SimpleNamespace()
    provider.download_attachment = AsyncMock(return_value=content)
    provider.send_email = AsyncMock(return_value="sent-message-id")
    return provider


def _message(
    *,
    headers: dict[str, str] | None = None,
    body_text: str = "",
    body_html: str | None = None,
) -> EmailMessage:
    return EmailMessage(
        id="mail-1",
        subject="测试邮件",
        sender="sender@example.com",
        sent_at="2026-07-23T00:00:00Z",
        body_text=body_text,
        body_html=body_html,
        headers=headers or {},
    )


def _one_click_headers(url: str) -> dict[str, str]:
    return {
        "List-Unsubscribe": f"<mailto:leave@example.com>, <{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "Authentication-Results": "mx.example; dkim=pass header.d=example.com",
        "DKIM-Signature": ("v=1; h=from:to:subject:list-unsubscribe:list-unsubscribe-post; b=test"),
    }


def _approval(
    approvals: ApprovalService,
    candidate: UnsubscribeCandidate,
    idempotency_key: str,
) -> str:
    action = (
        ApprovalAction.UNSUBSCRIBE_ONE_CLICK
        if candidate.method is UnsubscribeMethod.ONE_CLICK
        else ApprovalAction.UNSUBSCRIBE_MAILTO
    )
    return approvals.mint_after_interrupt(
        AuthContext(user_id="local-user"),
        action=action,
        target_id=candidate.fingerprint,
        payload=candidate.model_dump(mode="json"),
        idempotency_key=idempotency_key,
    )


def test_attachment_extracts_text_without_using_untrusted_path() -> None:
    provider = _mail_provider("安全正文".encode())
    attachment = _attachment(filename="../../private\\秘密.txt", size_bytes=12)

    result = asyncio.run(
        AttachmentTextService(max_attachment_bytes=1024).extract(provider, attachment)
    )

    assert result.status is AttachmentTextStatus.EXTRACTED
    assert result.filename == "秘密.txt"
    assert result.text == "安全正文"
    assert not Path("../../private/秘密.txt").exists()


def test_attachment_rejects_size_type_mismatch_and_unsupported_format() -> None:
    provider = _mail_provider()
    service = AttachmentTextService(max_attachment_bytes=10)

    oversized = asyncio.run(
        service.extract(provider, _attachment(filename="大文件.txt", size_bytes=11))
    )
    disguised = asyncio.run(
        service.extract(
            provider,
            _attachment(filename="伪装.pdf", content_type="text/plain"),
        )
    )
    unsupported = asyncio.run(
        service.extract(
            provider,
            _attachment(filename="程序.exe", content_type="application/octet-stream"),
        )
    )

    assert oversized.status is AttachmentTextStatus.SKIPPED
    assert disguised.status is AttachmentTextStatus.SKIPPED
    assert unsupported.status is AttachmentTextStatus.SKIPPED
    provider.download_attachment.assert_not_awaited()


def test_attachment_checks_actual_size_parser_failure_and_timeout() -> None:
    oversized_provider = _mail_provider(b"x" * 11)
    oversized = asyncio.run(
        AttachmentTextService(max_attachment_bytes=10).extract(
            oversized_provider,
            _attachment(size_bytes=1),
        )
    )
    malformed_pdf = asyncio.run(
        AttachmentTextService(max_attachment_bytes=1024).extract(
            _mail_provider(b"not-a-pdf"),
            _attachment(
                filename="损坏.pdf",
                content_type="application/pdf",
                size_bytes=9,
            ),
        )
    )

    def slow_parser(suffix: str, content: bytes) -> str:
        time.sleep(0.03)
        return "迟到的文本"

    timed_out = asyncio.run(
        AttachmentTextService(
            max_attachment_bytes=1024,
            timeout_seconds=0.001,
            extractor=slow_parser,
        ).extract(_mail_provider(), _attachment())
    )

    assert oversized.status is AttachmentTextStatus.SKIPPED
    assert malformed_pdf.status is AttachmentTextStatus.FAILED
    assert timed_out.status is AttachmentTextStatus.FAILED
    assert timed_out.reason == "附件解析超时"


def test_attachment_extracts_docx_and_truncates_output() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:document><w:p><w:t>这是一段很长的正文</w:t></w:p></w:document>",
        )
    attachment = _attachment(
        filename="报告.docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        size_bytes=len(buffer.getvalue()),
    )

    result = asyncio.run(
        AttachmentTextService(
            max_attachment_bytes=4096,
            max_text_chars=5,
        ).extract(_mail_provider(buffer.getvalue()), attachment)
    )

    assert result.status is AttachmentTextStatus.EXTRACTED
    assert result.truncated is True
    assert len(result.text) == 5


def test_unsubscribe_discovery_covers_one_click_mailto_website_and_unknown() -> None:
    url = "https://news.example.com/unsubscribe/token"
    header_candidates = discover_unsubscribe(_message(headers=_one_click_headers(url)))
    website = discover_unsubscribe(
        _message(body_html='<a href="https://news.example.com/unsubscribe/x">退订</a>')
    )
    unknown = discover_unsubscribe(_message())

    assert [candidate.method for candidate in header_candidates] == [
        UnsubscribeMethod.MAILTO,
        UnsubscribeMethod.ONE_CLICK,
    ]
    assert header_candidates[1].dkim_evidence is True
    assert website[0].method is UnsubscribeMethod.WEBSITE
    assert website[0].source is UnsubscribeSource.BODY
    assert unknown[0].method is UnsubscribeMethod.UNKNOWN


def test_one_click_requires_dkim_and_never_turns_website_into_post() -> None:
    headers = _one_click_headers("https://news.example.com/unsubscribe/token")
    headers.pop("Authentication-Results")

    candidate = discover_unsubscribe(_message(headers=headers))[1]

    assert candidate.method is UnsubscribeMethod.WEBSITE
    assert candidate.dkim_evidence is False


def test_one_click_executes_approved_post_and_blocks_duplicate(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    async def scenario() -> None:
        approvals = ApprovalService("a" * 32)
        store = JsonUnsubscribeStateStore(tmp_path / "unsubscribe.json")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UnsubscribeService(
                approvals=approvals,
                store=store,
                http_client=client,
            )
            candidate = discover_unsubscribe(
                _message(
                    headers=_one_click_headers("https://news.example.com/unsubscribe/private-token")
                )
            )[1]
            first_key = "unsubscribe-1"
            first = await service.execute(
                candidate,
                user_id="local-user",
                approval_token=_approval(approvals, candidate, first_key),
                idempotency_key=first_key,
            )
            second_key = "unsubscribe-2"
            second = await service.execute(
                candidate,
                user_id="local-user",
                approval_token=_approval(approvals, candidate, second_key),
                idempotency_key=second_key,
            )

        assert first.status is UnsubscribeResultStatus.CONFIRMED
        assert second.status is UnsubscribeResultStatus.ALREADY_SUBMITTED

    asyncio.run(scenario())

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].content == b"List-Unsubscribe=One-Click"
    persisted = (tmp_path / "unsubscribe.json").read_text()
    assert "private-token" not in persisted
    assert not list(tmp_path.glob(".unsubscribe.json.*"))


def test_mailto_and_manual_website_results(tmp_path: Path) -> None:
    async def scenario() -> None:
        approvals = ApprovalService("b" * 32)
        provider = _mail_provider()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as client:
            service = UnsubscribeService(
                approvals=approvals,
                store=JsonUnsubscribeStateStore(tmp_path / "state.json"),
                http_client=client,
                mail_provider=provider,
            )
            mailto = discover_unsubscribe(
                _message(
                    headers={
                        "List-Unsubscribe": (
                            "<mailto:leave@example.com?subject=remove&body=please>"
                        )
                    }
                )
            )[0]
            website = UnsubscribeCandidate(
                method=UnsubscribeMethod.WEBSITE,
                source=UnsubscribeSource.BODY,
                target="https://example.com/unsubscribe",
            )
            mailto_result = await service.execute(
                mailto,
                user_id="local-user",
                approval_token=_approval(approvals, mailto, "mailto-1"),
                idempotency_key="mailto-1",
            )
            website_result = await service.execute(
                website,
                user_id="local-user",
                approval_token="",
                idempotency_key="website-1",
            )

        assert mailto_result.status is UnsubscribeResultStatus.REQUEST_SENT
        assert website_result.status is UnsubscribeResultStatus.MANUAL_REQUIRED
        sent = provider.send_email.await_args.args[0]
        assert sent.to == ("leave@example.com",)
        assert (sent.subject, sent.body) == ("remove", "please")

    asyncio.run(scenario())


def test_batch_preserves_partial_failure_and_audit_evidence(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        status = 204 if request.url.host == "ok.example.com" else 500
        return httpx.Response(status, request=request, content=b"response")

    async def scenario() -> None:
        approvals = ApprovalService("c" * 32)
        store = JsonUnsubscribeStateStore(tmp_path / "state.json")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = UnsubscribeService(
                approvals=approvals,
                store=store,
                http_client=client,
            )
            candidates = [
                UnsubscribeCandidate(
                    method=UnsubscribeMethod.ONE_CLICK,
                    source=UnsubscribeSource.HEADER,
                    target=f"https://{host}/unsubscribe/token",
                    dkim_evidence=True,
                )
                for host in ("ok.example.com", "fail.example.com")
            ]
            requests = tuple(
                (candidate, _approval(approvals, candidate, f"batch-{index}"), f"batch-{index}")
                for index, candidate in enumerate(candidates)
            )
            results = await service.execute_many(requests, user_id="local-user")

        assert [result.status for result in results] == [
            UnsubscribeResultStatus.CONFIRMED,
            UnsubscribeResultStatus.FAILED,
        ]

    asyncio.run(scenario())

    payload = json.loads((tmp_path / "state.json").read_text())
    records = list(payload["records"].values())
    assert {record["state"] for record in records} == {"succeeded", "failed"}
    assert all(record["evidence_hash"] for record in records)
