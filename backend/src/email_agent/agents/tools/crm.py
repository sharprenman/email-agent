"""CRM 初始化、读取和审批画像更新 Tool。"""

from datetime import date
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from ...crm import CrmContactType, CrmPriority, CrmService


def build_crm_tools(crm: CrmService) -> tuple[BaseTool, ...]:
    async def initialize_crm(
        max_emails: int = 200,
        top_n: int = 20,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """聚合邮箱事实并初始化用户隔离的 CRM 联系人画像。"""
        result = await crm.initialize(
            max_emails=max_emails,
            top_n=top_n,
            exclude_domains=tuple(exclude_domains or ()),
        )
        return result.model_dump(mode="json")

    async def list_crm_contacts(
        limit: int = 50,
        needs_reply: bool = False,
    ) -> dict[str, Any]:
        """读取 CRM 联系人或只读取待回复联系人。"""
        items = await crm.list(limit=limit, needs_reply=needs_reply)
        return {
            "items": [item.model_dump(mode="json") for item in items],
            "count": len(items),
        }

    async def get_crm_contact(email: str) -> dict[str, Any]:
        """按邮箱读取一个 CRM 联系人画像。"""
        item = await crm.get(email)
        return {"item": item.model_dump(mode="json") if item else None}

    async def update_crm_contact(
        email: str,
        contact_type: CrmContactType | None = None,
        company: str | None = None,
        relationship: str | None = None,
        priority: CrmPriority | None = None,
        deal: str | None = None,
        next_contact_date: date | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """人工审批后保存结构化联系人画像字段。"""
        item = await crm.update_profile(
            email,
            contact_type=contact_type,
            company=company,
            relationship=relationship,
            priority=priority,
            deal=deal,
            next_contact_date=next_contact_date,
            tags=tuple(tags) if tags is not None else None,
            notes=notes,
        )
        return {"item": item.model_dump(mode="json")}

    return (
        StructuredTool.from_function(
            coroutine=initialize_crm,
            name="initialize_crm",
            description="人工审批后从邮箱事实初始化用户 CRM。",
        ),
        StructuredTool.from_function(
            coroutine=list_crm_contacts,
            name="list_crm_contacts",
            description="读取 CRM 联系人；可只返回待回复联系人。",
        ),
        StructuredTool.from_function(
            coroutine=get_crm_contact,
            name="get_crm_contact",
            description="按邮箱读取一个 CRM 联系人画像。",
        ),
        StructuredTool.from_function(
            coroutine=update_crm_contact,
            name="update_crm_contact",
            description="人工审批后更新 CRM 联系人画像。",
        ),
    )
