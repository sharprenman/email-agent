"""邮件草稿、审批发信和审批退订 Tool。"""

import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from ...calendar import ApprovalAction, ApprovalService
from ...config import AuthContext
from ...content_tools import UnsubscribeCandidate, UnsubscribeService
from ...contracts import MailProvider, SendEmailRequest
from ...crm import CrmService

logger = logging.getLogger(__name__)


def mail_approval_payload(request: SendEmailRequest) -> dict[str, Any]:
    """生成发信审批与实际执行共同使用的规范化载荷。"""
    return request.model_dump(mode="json")


class ApprovedMailService:
    """在调用 MailProvider 前消费绑定请求的一次性审批凭证。"""

    def __init__(
        self,
        provider: MailProvider,
        approvals: ApprovalService,
        auth: AuthContext,
        crm: CrmService | None = None,
    ) -> None:
        self._provider = provider
        self._approvals = approvals
        self._auth = auth
        self._crm = crm

    async def send(
        self,
        request: SendEmailRequest,
        *,
        approval_token: str,
        idempotency_key: str,
    ) -> str:
        """审批内容完全匹配后执行一次发信。"""
        self._approvals.consume(
            approval_token,
            user_id=self._auth.user_id,
            action=ApprovalAction.SEND_EMAIL,
            target_id=request.reply_to_email_id,
            payload=mail_approval_payload(request),
            idempotency_key=idempotency_key,
        )
        message_id = await self._provider.send_email(
            request,
            idempotency_key=idempotency_key,
        )
        if self._crm is not None:
            try:
                await self._crm.sync_after_send(request.to)
            except Exception as exc:
                logger.warning(
                    "CRM 发信后同步失败",
                    extra={"error_type": type(exc).__name__},
                )
        return message_id


def build_mail_writer_tools(
    mail_writes: ApprovedMailService,
    unsubscribe_service: UnsubscribeService | None,
    auth: AuthContext,
) -> tuple[BaseTool, ...]:
    """构建草稿以及受审批保护的邮件副作用 Tool。"""

    async def prepare_email_draft(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to_email_id: str | None = None,
    ) -> dict[str, Any]:
        """校验并返回尚未发送的结构化邮件草稿。"""
        request = _send_request(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_email_id=reply_to_email_id,
        )
        return {
            "status": "draft",
            "sent": False,
            "email": request.model_dump(mode="json"),
        }

    async def send_email(
        to: list[str],
        subject: str,
        body: str,
        idempotency_key: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to_email_id: str | None = None,
        approval_token: str = "",
    ) -> dict[str, Any]:
        """使用 API 恢复层注入的一次性审批凭证发送邮件。"""
        request = _send_request(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            reply_to_email_id=reply_to_email_id,
        )
        message_id = await mail_writes.send(
            request,
            approval_token=approval_token,
            idempotency_key=idempotency_key,
        )
        return {"status": "sent", "message_id": message_id}

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            coroutine=prepare_email_draft,
            name="prepare_email_draft",
            description="生成并校验草稿，不产生外部副作用。",
        ),
        StructuredTool.from_function(
            coroutine=send_email,
            name="send_email",
            description="人工审批后发送邮件；未注入审批凭证时必定失败。",
        ),
    ]
    if unsubscribe_service is not None:

        async def execute_unsubscribe(
            candidate: UnsubscribeCandidate,
            idempotency_key: str,
            approval_token: str = "",
        ) -> dict[str, Any]:
            """使用 API 恢复层注入的一次性凭证执行退订。"""
            result = await unsubscribe_service.execute(
                candidate,
                user_id=auth.user_id,
                approval_token=approval_token,
                idempotency_key=idempotency_key,
            )
            return result.model_dump(mode="json")

        tools.append(
            StructuredTool.from_function(
                coroutine=execute_unsubscribe,
                name="execute_unsubscribe",
                description="人工审批后执行 one-click 或 mailto 退订。",
            )
        )
    return tuple(tools)


def _send_request(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None,
    bcc: list[str] | None,
    reply_to_email_id: str | None,
) -> SendEmailRequest:
    return SendEmailRequest(
        to=tuple(to),
        subject=subject,
        body=body,
        cc=tuple(cc or ()),
        bcc=tuple(bcc or ()),
        reply_to_email_id=reply_to_email_id,
    )
