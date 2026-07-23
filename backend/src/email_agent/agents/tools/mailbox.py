"""只读邮箱、附件、联系人和退订发现 Tool。"""

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from ...content_tools import AttachmentTextService, discover_unsubscribe
from ...contracts import MailProvider, ProviderNotFoundError


def build_mailbox_tools(
    provider: MailProvider,
    attachment_service: AttachmentTextService,
) -> tuple[BaseTool, ...]:
    """构建不具备任何外部写入能力的邮箱 Tool。"""

    async def get_mailbox_identity() -> dict[str, Any]:
        """读取当前可信邮箱身份。"""
        return (await provider.get_identity()).model_dump(mode="json")

    async def read_inbox(limit: int = 20, unread_only: bool = False) -> list[dict[str, Any]]:
        """读取收件箱摘要。"""
        return [
            item.model_dump(mode="json")
            for item in await provider.read_inbox(limit=limit, unread_only=unread_only)
        ]

    async def search_emails(query: str, limit: int = 20) -> list[dict[str, Any]]:
        """按供应商查询语法搜索邮件。"""
        return [
            item.model_dump(mode="json")
            for item in await provider.search_emails(query=query, limit=limit)
        ]

    async def get_email(email_id: str) -> dict[str, Any]:
        """读取完整邮件正文与标准头信息。"""
        return (await provider.get_email(email_id)).model_dump(mode="json")

    async def get_sent_emails(limit: int = 20) -> list[dict[str, Any]]:
        """读取已发送邮件摘要。"""
        return [
            item.model_dump(mode="json") for item in await provider.get_sent_emails(limit=limit)
        ]

    async def get_unanswered_emails(limit: int = 20) -> list[dict[str, Any]]:
        """读取仍等待当前用户回复的邮件摘要。"""
        return [
            item.model_dump(mode="json")
            for item in await provider.get_unanswered_emails(limit=limit)
        ]

    async def list_email_attachments(email_id: str) -> list[dict[str, Any]]:
        """读取邮件附件元数据，不解析附件正文。"""
        return [item.model_dump(mode="json") for item in await provider.list_attachments(email_id)]

    async def extract_attachment_text(
        email_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:
        """在安全限制内提取一个已知附件的文本。"""
        attachments = await provider.list_attachments(email_id)
        attachment = next((item for item in attachments if item.id == attachment_id), None)
        if attachment is None:
            raise ProviderNotFoundError("邮件附件不存在")
        return (await attachment_service.extract(provider, attachment)).model_dump(mode="json")

    async def list_contacts(limit: int = 100) -> list[dict[str, Any]]:
        """读取邮箱联系人。"""
        return [item.model_dump(mode="json") for item in await provider.list_contacts(limit=limit)]

    async def discover_email_unsubscribe(email_id: str) -> list[dict[str, Any]]:
        """只读发现邮件支持的退订方式，不执行任何退订请求。"""
        message = await provider.get_email(email_id)
        return [item.model_dump(mode="json") for item in discover_unsubscribe(message)]

    definitions = (
        (get_mailbox_identity, "get_mailbox_identity", "读取当前邮箱身份。"),
        (read_inbox, "read_inbox", "读取收件箱邮件摘要。"),
        (search_emails, "search_emails", "搜索邮箱中的邮件。"),
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
        )
        for function, name, description in definitions
    )
