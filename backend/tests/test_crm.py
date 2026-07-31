"""CRM 初始化、用户隔离、画像更新和发信后同步测试。"""

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from email_agent.agents.tools.mail_writer import ApprovedMailService
from email_agent.calendar import ApprovalAction, ApprovalService
from email_agent.config import AuthContext
from email_agent.contracts import (
    Contact,
    EmailSummary,
    MailboxIdentity,
    SendEmailRequest,
)
from email_agent.crm import CrmContactType, CrmPriority, CrmService
from email_agent.persistence import build_in_memory_persistence


def _summary(
    message_id: str,
    sender: str,
    *,
    recipients: tuple[str, ...] = (),
    day: int = 1,
) -> EmailSummary:
    return EmailSummary(
        id=message_id,
        subject="测试",
        sender=sender,
        recipients=recipients,
        sent_at=datetime(2026, 7, day, tzinfo=UTC),
    )


def _provider():
    return SimpleNamespace(
        get_identity=AsyncMock(
            return_value=MailboxIdentity(email="owner@example.com", display_name="Owner")
        ),
        list_contacts=AsyncMock(
            return_value=(
                Contact(email="Alice <alice@partner.example>", display_name="Alice"),
                Contact(email="notify@service.example", display_name="Service"),
            )
        ),
        read_inbox=AsyncMock(
            return_value=(
                _summary("in-1", "Alice <alice@partner.example>", day=2),
                _summary("in-2", "notify@service.example", day=3),
            )
        ),
        get_sent_emails=AsyncMock(
            return_value=(
                _summary(
                    "sent-1",
                    "owner@example.com",
                    recipients=("alice@partner.example",),
                    day=4,
                ),
            )
        ),
        get_unanswered_emails=AsyncMock(
            return_value=(_summary("wait-1", "alice@partner.example", day=5),)
        ),
        send_email=AsyncMock(return_value="message-1"),
    )


def test_crm_initialization_profiles_contacts_and_isolates_users() -> None:
    persistence = build_in_memory_persistence()
    provider = _provider()
    owner = CrmService(provider, persistence.state, AuthContext(user_id="owner"))
    other = CrmService(provider, persistence.state, AuthContext(user_id="other"))

    result = asyncio.run(owner.initialize(max_emails=50, top_n=10))
    needs_reply = asyncio.run(owner.list(needs_reply=True))

    assert result.contacts_saved == 2
    assert result.needs_reply == 1
    assert needs_reply[0].email == "alice@partner.example"
    assert needs_reply[0].contact_type is CrmContactType.PERSON
    assert needs_reply[0].priority is CrmPriority.HIGH
    assert needs_reply[0].company == "partner.example"
    assert asyncio.run(other.list()) == ()


def test_crm_profile_update_preserves_mailbox_facts() -> None:
    persistence = build_in_memory_persistence()
    crm = CrmService(_provider(), persistence.state, AuthContext(user_id="owner"))
    asyncio.run(crm.initialize())

    updated = asyncio.run(
        crm.update_profile(
            "alice@partner.example",
            relationship="客户",
            priority=CrmPriority.HIGH,
            deal="续约",
            next_contact_date=date(2026, 8, 10),
            tags=("客户", "重点"),
            notes="用户确认的业务关系",
        )
    )

    assert updated.relationship == "客户"
    assert updated.frequency == 3
    assert updated.needs_reply is True
    assert updated.tags == ("客户", "重点")


def test_successful_send_syncs_crm_but_failed_send_does_not() -> None:
    persistence = build_in_memory_persistence()
    provider = _provider()
    auth = AuthContext(user_id="owner")
    crm = CrmService(provider, persistence.state, auth)
    approvals = ApprovalService("a" * 32)
    service = ApprovedMailService(provider, approvals, auth, crm)
    request = SendEmailRequest(
        to=("new-contact@example.net",),
        subject="你好",
        body="正文",
    )
    key = "send-operation-0001"
    token = approvals.mint_after_interrupt(
        auth,
        action=ApprovalAction.SEND_EMAIL,
        target_id=None,
        payload=request.model_dump(mode="json"),
        idempotency_key=key,
    )

    message_id = asyncio.run(
        service.send(request, approval_token=token, idempotency_key=key)
    )
    contact = asyncio.run(crm.get("new-contact@example.net"))

    assert message_id == "message-1"
    assert contact is not None
    assert contact.frequency == 1
    assert contact.needs_reply is False

    provider.send_email.side_effect = RuntimeError("发送失败")
    failed_request = SendEmailRequest(
        to=("failed@example.net",),
        subject="失败",
        body="正文",
    )
    failed_key = "send-operation-0002"
    failed_token = approvals.mint_after_interrupt(
        auth,
        action=ApprovalAction.SEND_EMAIL,
        target_id=None,
        payload=failed_request.model_dump(mode="json"),
        idempotency_key=failed_key,
    )
    try:
        asyncio.run(
            service.send(
                failed_request,
                approval_token=failed_token,
                idempotency_key=failed_key,
            )
        )
    except RuntimeError:
        pass
    assert asyncio.run(crm.get("failed@example.net")) is None
