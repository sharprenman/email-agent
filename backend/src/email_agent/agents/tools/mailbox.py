"""只读邮箱、附件、联系人和退订发现 Tool。"""

import asyncio
from collections.abc import Awaitable
from datetime import datetime
from typing import Any, TypeVar

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from ...content_tools import AttachmentTextService, discover_unsubscribe
from ...contracts import EmailSearchCriteria, MailProvider, ProviderError, ProviderNotFoundError
from ...skills import prepare_skill_workflow

_T = TypeVar("_T")


async def _provider_call(awaitable: Awaitable[_T]) -> _T:
    try:
        return await awaitable
    except ProviderError as exc:
        raise ToolException(f"邮箱 Provider 调用失败（{exc.code}）") from exc


def build_mailbox_tools(
    provider: MailProvider,
    attachment_service: AttachmentTextService,
    *,
    user_timezone: str,
) -> tuple[BaseTool, ...]:
    """构建不具备任何外部写入能力的邮箱 Tool。"""

    async def get_mailbox_identity() -> dict[str, Any]:
        """读取当前可信邮箱身份。"""
        return (await _provider_call(provider.get_identity())).model_dump(mode="json")

    async def read_inbox(limit: int = 20, unread_only: bool = False) -> dict[str, Any]:
        """读取收件箱摘要。"""
        items = [
            item.model_dump(mode="json")
            for item in await _provider_call(
                provider.read_inbox(limit=limit, unread_only=unread_only)
            )
        ]
        return {"items": items, "count": len(items)}

    async def search_skill_emails(
        skill_name: str,
        days: int | None = None,
        max_results: int | None = None,
        query: str | None = None,
        include_unanswered: bool = False,
    ) -> dict[str, Any]:
        """在服务端重建受限 Skill 计划后搜索，避免模型改写 Provider 查询条件。"""
        plan = prepare_skill_workflow(
            skill_name,
            days=days,
            max_results=max_results,
            query=query,
            timezone=user_timezone,
        )
        if plan.search_criteria is None:
            raise ValueError(f"Skill {skill_name} 不包含邮件搜索步骤")
        unanswered_items: list[dict[str, Any]] | None = None
        if include_unanswered:
            if "get_unanswered_emails" not in plan.delegated_tools:
                raise ValueError(f"Skill {skill_name} 不包含待回复邮件步骤")
        search_call = provider.search_emails(
            criteria=plan.search_criteria,
            limit=plan.max_results,
        )
        if include_unanswered:
            searched, unanswered = await _provider_call(
                asyncio.gather(
                    search_call,
                    provider.get_unanswered_emails(
                        limit=plan.max_results,
                        since=plan.window_start,
                    ),
                )
            )
            unanswered_items = [item.model_dump(mode="json") for item in unanswered]
        else:
            searched = await _provider_call(search_call)
        items = [item.model_dump(mode="json") for item in searched]
        result = {
            "items": items,
            "count": len(items),
            "criteria": plan.search_criteria.model_dump(mode="json"),
            "window_start": plan.window_start.isoformat() if plan.window_start else None,
            "window_end": plan.window_end.isoformat() if plan.window_end else None,
        }
        if unanswered_items is not None:
            result["unanswered"] = {
                "items": unanswered_items,
                "count": len(unanswered_items),
            }
        return result

    async def search_emails(
        criteria: EmailSearchCriteria,
        limit: int = 20,
    ) -> dict[str, Any]:
        """使用 Provider 无关条件搜索邮件。"""
        items = [
            item.model_dump(mode="json")
            for item in await _provider_call(
                provider.search_emails(criteria=criteria, limit=limit)
            )
        ]
        return {"items": items, "count": len(items)}

    async def get_email(email_id: str) -> dict[str, Any]:
        """读取完整邮件正文与标准头信息。"""
        return (await _provider_call(provider.get_email(email_id))).model_dump(mode="json")

    async def get_sent_emails(limit: int = 20) -> dict[str, Any]:
        """读取已发送邮件摘要。"""
        items = [
            item.model_dump(mode="json")
            for item in await _provider_call(provider.get_sent_emails(limit=limit))
        ]
        return {"items": items, "count": len(items)}

    async def get_unanswered_emails(
        limit: int = 20,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """读取仍等待当前用户回复的邮件摘要。"""
        items = [
            item.model_dump(mode="json")
            for item in await _provider_call(
                provider.get_unanswered_emails(limit=limit, since=since)
            )
        ]
        return {"items": items, "count": len(items)}

    async def list_email_attachments(email_id: str) -> dict[str, Any]:
        """读取邮件附件元数据，不解析附件正文。"""
        items = [
            item.model_dump(mode="json")
            for item in await _provider_call(provider.list_attachments(email_id))
        ]
        return {"items": items, "count": len(items)}

    async def extract_attachment_text(
        email_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:
        """在安全限制内提取一个已知附件的文本。"""
        attachments = await _provider_call(provider.list_attachments(email_id))
        attachment = next((item for item in attachments if item.id == attachment_id), None)
        if attachment is None:
            raise ProviderNotFoundError("邮件附件不存在")
        return (await attachment_service.extract(provider, attachment)).model_dump(mode="json")

    async def list_contacts(limit: int = 100) -> dict[str, Any]:
        """读取邮箱联系人。"""
        items = [
            item.model_dump(mode="json")
            for item in await _provider_call(provider.list_contacts(limit=limit))
        ]
        return {"items": items, "count": len(items)}

    async def discover_email_unsubscribe(email_id: str) -> dict[str, Any]:
        """只读发现邮件支持的退订方式，不执行任何退订请求。"""
        message = await _provider_call(provider.get_email(email_id))
        items = [item.model_dump(mode="json") for item in discover_unsubscribe(message)]
        return {"items": items, "count": len(items)}

    definitions = (
        (get_mailbox_identity, "get_mailbox_identity", "读取当前邮箱身份。"),
        (read_inbox, "read_inbox", "读取收件箱邮件摘要。"),
        (search_emails, "search_emails", "搜索邮箱中的邮件。"),
        (
            search_skill_emails,
            "search_skill_emails",
            "按服务端受限 Skill 计划搜索邮件，模型不能改写底层查询条件。",
        ),
        (get_email, "get_email", "读取一封完整邮件。"),
        (get_sent_emails, "get_sent_emails", "读取已发送邮件。"),
        (get_unanswered_emails, "get_unanswered_emails", "读取等待回复的邮件。"),
        (list_email_attachments, "list_email_attachments", "列出邮件附件元数据。"),
        (
            extract_attachment_text,
            "extract_attachment_text",
            "安全提取一个白名单附件的文本。",
        ),
        (list_contacts, "list_contacts", "读取邮箱联系人。"),
        (
            discover_email_unsubscribe,
            "discover_email_unsubscribe",
            "发现退订方式但不执行退订。",
        ),
    )
    return tuple(
        StructuredTool.from_function(
            coroutine=function,
            name=name,
            description=description,
            handle_tool_error=True,
        )
        for function, name, description in definitions
    )
